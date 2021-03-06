# -*- coding: utf-8 -*-
#
# Copyright (C) 2010-2016 PPMessage.
# Guijin Ding, dingguijin@gmail.com.
# 
# send/proc.py
#

from ppmessage.core.imageconverter import ImageConverter

from ppmessage.core.constant import REDIS_DISPATCHER_NOTIFICATION_KEY
from ppmessage.core.constant import REDIS_ACK_NOTIFICATION_KEY
from ppmessage.core.constant import MESSAGE_MAX_TEXT_LEN
from ppmessage.core.constant import MESSAGE_SUBTYPE
from ppmessage.core.constant import MESSAGE_TYPE
from ppmessage.core.constant import THUMBNAIL_HEIGHT
from ppmessage.core.constant import THUMBNAIL_WIDTH
from ppmessage.core.constant import CONVERSATION_TYPE
from ppmessage.core.constant import CONVERSATION_STATUS
from ppmessage.core.constant import PCSOCKET_SRV
from ppmessage.core.constant import TASK_STATUS
from ppmessage.core.constant import DIS_WHAT
from ppmessage.core.constant import YVOBJECT

from ppmessage.db.models import MessagePushTask
from ppmessage.db.models import ConversationInfo
from ppmessage.db.models import ConversationUserData
from ppmessage.db.models import FileInfo
from ppmessage.db.models import DeviceInfo
from ppmessage.db.models import DeviceUser

from ppmessage.core.utils.filemanager import create_file_with_data
from ppmessage.core.utils.filemanager import read_file

from ppmessage.core.redis import redis_hash_to_dict

import json
import uuid
import time
import logging
import datetime
from PIL import Image

class Proc():
    
    def __init__(self, _app):
        self._redis = _app.redis
        self._subtype_parsers = {}
        return

    def register_subtypes(self, _subtypes):
        self._subtype_parsers = {}
        for _i in _subtypes:
            self._subtype_parsers[_i] = getattr(self, "_parse_" + _i.upper(), None)
        return
    
    def check(self, _body):
        self._body = _body
        if not isinstance(_body, dict):
            self._body = json.loads(_body)
        
        self._uuid = self._body.get("uuid")
        self._to_type = self._body.get("to_type")
        self._to_uuid = self._body.get("to_uuid")
        self._from_type = self._body.get("from_type")
        self._from_uuid = self._body.get("from_uuid")
        self._conversation_uuid = self._body.get("conversation_uuid")
        self._conversation_type = self._body.get("conversation_type")
        self._message_body = self._body.get("message_body")
        self._from_device_uuid = self._body.get("device_uuid")
        self._message_type = self._body.get("message_type")
        self._message_subtype = self._body.get("message_subtype")

        self._pcsocket = self._body.get("pcsocket")
        
        if self._uuid == None or \
           self._to_type == None or \
           self._to_uuid == None or \
           self._from_type == None or \
           self._from_uuid == None or \
           self._conversation_uuid == None or \
           self._message_type == None or \
           self._message_subtype == None or \
           self._message_body == None:
            logging.error("send message failed for input.")
            return False
        return True

    def parse(self):
        self._message_type = self._message_type.upper()
        self._message_subtype = self._message_subtype.upper()
        if isinstance(self._message_body, unicode):
            self._message_body = self._message_body.encode("utf-8")

        _parser = self._subtype_parsers.get(self._message_subtype)
        if _parser == None:
            logging.error("unsupport message: %s" % self._body)
            return False
        return _parser()

    def save(self):
        _task = {
            "uuid": self._uuid,
            "conversation_uuid": self._conversation_uuid,
            "conversation_type": self._conversation_type,
            "message_type": self._message_type,
            "message_subtype": self._message_subtype,
            "from_uuid": self._from_uuid,
            "from_type": self._from_type,
            "from_device_uuid": self._from_device_uuid,
            "to_uuid": self._to_uuid,
            "to_type": self._to_type,
            "body": self._message_body,
            "task_status": TASK_STATUS.PENDING,
        }
        _row = MessagePushTask(**_task)
        _row.async_add(self._redis)
        _row.create_redis_keys(self._redis)

        _row = ConversationInfo(uuid=self._conversation_uuid, latest_task=self._uuid)
        _row.async_update(self._redis)
        _row.update_redis_keys(self._redis)

        _m = {"task_uuid": self._uuid}
        self._redis.rpush(REDIS_DISPATCHER_NOTIFICATION_KEY, json.dumps(_m))

        _key = ConversationUserData.__tablename__ + ".conversation_uuid." + self._conversation_uuid + ".datas"
        _datas = self._redis.smembers(_key)
        for _data_uuid in _datas:
            _row = ConversationUserData(uuid=_data_uuid, conversation_status=CONVERSATION_STATUS.OPEN)
            _row.async_update(self._redis)
            _row.update_redis_keys(self._redis)
        
        # for message routing algorithm
        self._user_latest_send_message_time()
        return

    def _user_latest_send_message_time(self):
        _now = datetime.datetime.now()
        _row = DeviceUser(uuid=self._from_uuid, latest_send_message_time=_now)
        _row.async_update(self._redis)
        return

    def _parse_TEXT(self):
        if len(self._message_body) > MESSAGE_MAX_TEXT_LEN:
            _fid = create_file_with_data(self._redis, self._message_body, "text/plain", self._from_uuid)
            self._message_subtype = MESSAGE_SUBTYPE.TXT
            self._message_body = json.dumps({"fid": _fid})
        return True

    def _parse_IMAGE(self):
        _image = self._parseImage(self._message_body)
        if _image == None:
            return False
        self._message_body = json.dumps(_image)
        return True

    def _parseImage(self, _body):
        _image = json.loads(_body)

        _fid = _image.get("fid")
        _mime = _image.get("mime")

        if _fid == None or _mime == None:
            logging.error("Error for message body of image message")
            return None
        
        _mime = _mime.lower()
        if _mime not in ["image/jpg", "image/jpeg", "image/png", "image/gif"]:
            logging.error("Error for not supported mime=%s." % (_mime))
            return None

        _file = redis_hash_to_dict(self._redis, FileInfo, _fid)
        if _file == None:
            logging.error("Error for no file in redis: %s" % _fid)
            return None

        _image = None
        try:
            # raise IOError when file not image
            _image = Image.open(_file["file_path"])
        except:
            pass
        finally:
            if _image == None:
                logging.error("PIL can not identify the file_id=%s, not image." % (_fid))
                return None

        _image_width, _image_height = _image.size
        _thum_width = _image_width
        _thum_height = _image_height
        
        if _image.format == "GIF":
            return {"thum":_fid, "orig":_fid, "mime":"image/gif", "orig_width": _image_width, "orig_height": _image_height, "thum_width": _thum_width, "thum_height": _thum_height}
        
        _thum_format = "JPEG"
        if _image.format == "PNG":
            _thum_format = "PNG"

        _thum_image_info = ImageConverter.thumbnailByKeepImage(_image, _thum_format)
        _thum_data = _thum_image_info["data"]
        _thum_image = _thum_image_info["image"]
        if _thum_data == None:
            logging.error("Error for thumbnail image")
            return None

        _thum_id = create_file_with_data(self._redis, _thum_data, _mime, self._from_uuid)

        _thum_width, _thum_height = _thum_image.size

        # where assume the _thum must be jpeg
        return {"thum":_thum_id, "orig":_fid, "mime":_mime, "orig_width": _image_width, "orig_height": _image_height, "thum_width": _thum_width, "thum_height": _thum_height}

    def ack(self, _code):
        if self._pcsocket == None:
            return
        _device_uuid = self._pcsocket.get("device_uuid")
        if _device_uuid == None:
            return
        _body = {
            "device_uuid": _device_uuid,
            "what": DIS_WHAT.SEND,
            "code": _code,
            "extra": {
                "uuid": self._uuid,
                "conversation_uuid": self._conversation_uuid
            },
        }
        _key = REDIS_ACK_NOTIFICATION_KEY
        self._redis.rpush(_key, json.dumps(_body))
        return
    
