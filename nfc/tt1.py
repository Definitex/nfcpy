# -*- coding: latin-1 -*-
# -----------------------------------------------------------------------------
# Copyright 2011-2013
#   Stephen Tiedemann <stephen.tiedemann@googlemail.com>, 
#   Alexander Knaub <sanyok.og@googlemail.com>
#
# Licensed under the EUPL, Version 1.1 or - as soon they 
# will be approved by the European Commission - subsequent
# versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the
# Licence.
# You may obtain a copy of the Licence at:
#
# http://www.osor.eu/eupl
#
# Unless required by applicable law or agreed to in
# writing, software distributed under the Licence is
# distributed on an "AS IS" basis,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied.
# See the Licence for the specific language governing
# permissions and limitations under the Licence.
# -----------------------------------------------------------------------------
# BUGS:
# - does not handle type 1 tags with dynamic memory layout
# -----------------------------------------------------------------------------

import logging
log = logging.getLogger(__name__)

import tag
import nfc.clf

class NDEF(tag.NDEF):
    def __init__(self, tag):
        self._tag = tag
        self._msg = None
        self._cc = tag[8:12]
        if not self._cc[0] == 0xE1:
            raise ValueError("wrong ndef magic number")
        if not self._cc[3] & 0xF0 == 0:
            raise ValueError("no ndef read permissions")
        self._skip = set(range(104, 120))
        offset = 12
        while offset is not None:
            offset = self._read_tlv(offset)

    def _read_tlv(self, offset):
        read_tlv = {
            0x00: lambda x: x + 1,
            0x01: self._read_lock_tlv,
            0x02: self._read_memory_tlv,
            0x03: self._read_ndef_tlv,
            0xFE: lambda x: None
            }.get(self._tag[offset], self._read_unknown_tlv)
        return read_tlv(offset + 1)

    def _read_unknown_tlv(self, offset):
        log.debug("found unknown tlv")
        length, offset = self._read_tlv_length(offset)
        return offset + length
        
    def _read_ndef_tlv(self, offset):
        log.debug("ndef message tlv at 0x{0:0X}".format(offset-1))
        self._ndef_tlv_offset = offset - 1
        length, offset = self._read_tlv_length(offset)
        log.debug("ndef message length is {0}".format(length))
        self._capacity = (self._cc[2]+1)*8 - offset - len(self._skip)
        if length < 255 and self._capacity >= 255:
            self._capacity -= 2 # account for three byte length format
        self._msg = bytearray()
        while length > 0:
            if not offset in self._skip:
                self._msg.append(self._tag[offset])
                length -= 1
            offset += 1
        return None
    
    def _read_lock_tlv(self, offset):
        log.debug("dynamic lock byte tlv at 0x{0:0X}".format(offset-1))
        length, offset = self._read_tlv_length(offset)
        value = self._tag[offset:offset+length]
        page_offs = value[0] >> 4
        byte_offs = value[0] & 0x0F
        resv_size = ((value[1] - 1) / 8) + 1
        page_size = 2 ** (value[2] & 0x0F)
        resv_start = page_offs * page_size + byte_offs
        self._skip.update(range(resv_start, resv_start + resv_size))
        return offset + length

    def _read_memory_tlv(self, offset):
        log.debug("memory control tlv at 0x{0:0X}".format(offset-1))
        length, offset = self._read_tlv_length(offset)
        value = self._tag[offset:offset+length]
        page_offs = value[0] >> 4
        byte_offs = value[0] & 0x0F
        resv_size = value[1]
        page_size = 2 ** (value[2] & 0x0F)
        resv_start = page_offs * page_size + byte_offs
        self._skip.update(range(resv_start, resv_start + resv_size))
        return offset + length

    def _read_tlv_length(self, offset):
        length = self._tag[offset]
        if length == 255:
            length = self._tag[offset+1] * 256 + self._tag[offset+2];
            offset = offset + 2
            if length < 256 or length == 0xFFFF:
                raise ValueError("invalid tlv lenght value")
        return length, offset + 1
        
    @property
    def version(self):
        """The version of the NDEF mapping."""
        return "%d.%d" % (self._cc[1]>>4, self._cc[1]&0x0F)

    @property
    def capacity(self):
        """The maximum number of user bytes on the NDEF tag."""
        return self._capacity

    @property
    def writeable(self):
        """Is True if new data can be written to the NDEF tag."""
        return self._cc[3] == 0x00

    @property
    def message(self):
        """A character string containing the NDEF message data."""
        return str(self._msg)

    @message.setter
    def message(self, data):
        if not self.writeable:
            raise IOError("tag writing disabled")
        if len(data) > self.capacity:
            raise IOError("ndef message beyond tag capacity")
        self._msg = bytearray(data)
        if len(data) < self.capacity:
            data = data + "\xFE"
        data = bytearray(data)
        with self._tag as tag:
            tag[0x08] = 0x00
            tag[0x09] = 0x10
            tag[0x0B] = 0x00
            offset = self._ndef_tlv_offset + 1
            if len(self._msg) < 255:
                tag[offset] = len(self._msg)
                offset += 1
            else:
                tag[offset] = 255
                tag[offset+1] = len(self._msg) / 256
                tag[offset+2] = len(self._msg) % 256
                offset += 3
            for octet in data:
                while offset in self._skip:
                    offset += 1
                tag[offset] = octet
                offset += 1
        with self._tag as tag:
            tag[8] = 0xE1

class Type1Tag(tag.TAG):
    def __init__(self, clf, data):
        self.clf = clf
        self.uid = data["UID"]
        self._mmap = self.read_all()[2:]
        self._sync = set()
        try: self._ndef = NDEF(self)
        except Exception as e:
            log.error("while reading ndef: " + str(e))

    def __str__(self):
        return "Type1Tag UID=" + str(self.uid).encode("hex")

    def __getitem__(self, key):
        if type(key) is type(int()):
            key = slice(key, key+1)
        if not type(key) is type(slice(1)):
            raise TypeError("key must be of type int or slice")
        if key.start > key.stop:
            raise ValueError("start index is greater than stop index")
        if key.stop > len(self._mmap):
            for block in range(len(self._mmap)/8, key.stop/8 + 1):
                self._mmap += self.read_block(block)
        bytes = self._mmap[key.start:key.stop]
        return bytes if len(bytes) > 1 else bytes[0]
        
    def __setitem__(self, key, value):
        if type(key) is type(int()):
            key = slice(key, key+1)
        if type(key) is not type(slice(1)):
            raise TypeError("key must be of type int or slice")
        if type(value) == type(int()):
            value = bytearray([value])
        else:
            value = bytearray(value)
        if len(value) != key.stop - key.start:
            raise ValueError("value and slice length do not match")
        if key.stop > len(self._mmap):
            self.__getitem__(key)
        for i in xrange(key.start, key.stop):
            self._mmap[i] = value[i-key.start]
            self._sync.add(i)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            if self._mmap[10] < 15:
                for i in sorted(self._sync):
                    self.write_byte(i, self._mmap[i])
                self._sync.clear()
            else:
                while len(self._sync) > 0:
                    block = sorted(self._sync).pop(0) / 8
                    self.write_block(block, self._mmap[block<<3:(block+1)<<3])
                    self._sync -= set(range(block<<3, (block+1)<<3))
        
    @property
    def _is_present(self):
        """Returns True if the tag is still within communication range."""
        try: return bool(self.read_byte(0))
        except nfc.clf.DigitalProtocolError: return False

    def transceive(self, data, timeout=0.1):
        return self.clf.dev.transceive(data, timeout, check_crc=True)

    def read_all(self):
        """Read header rom and all static memory bytes (blocks 0-14).
        """
        log.debug("read all")
        cmd = "\x00\x00\x00" + self.uid
        return self.transceive(cmd)

    def read_byte(self, addr):
        """Read a single byte from static memory area (blocks 0-14).
        """
        log.debug("read byte at address 0x{0:03X}".format(addr))
        cmd = "\x01" + chr(addr) + "\x00" + self.uid
        return self.transceive(cmd)[1]

    def write_byte(self, addr, byte, erase=True):
        """Write a single byte to static memory area (blocks 0-14).
        The target byte is zero'd first if 'erase' is True (default).
        """
        log.debug("write byte at address 0x{0:03X}".format(addr))
        cmd = "\x53" if erase is True else "\x1A"
        cmd = cmd + chr(addr) + chr(byte) + self.uid
        return self.transceive(cmd)

    def read_block(self, block):
        """Read an 8-byte data block at address (block * 8).
        """
        log.debug("read block at address 0x{0:03X}".format(block*8))
        cmd = "\x02" + chr(block) + 8 * chr(0) + self.uid
        return self.transceive(cmd)[1:9]

    def write_block(self, block, data, erase=True):
        """Write an 8-byte data block at address (block * 8).
        The target bytes are zero'd first if 'erase' is True (default).
        """
        log.debug("write block at address 0x{0:03X}".format(block*8))
        cmd = "\x54" if erase is True else "\x1B"
        cmd = cmd + chr(block) + data + self.uid
        return self.transceive(cmd)
