"""Bounded Protobuf wire and text decoding, without application I/O."""
from __future__ import annotations

import re


def decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    if pos < 0:
        raise ValueError('negative protobuf offset')
    result = 0
    for shift in range(0, 70, 7):
        if pos >= len(buf):
            raise ValueError('truncated protobuf varint')
        byte = buf[pos]
        pos += 1
        if shift == 63 and byte > 1:
            raise ValueError('protobuf varint overflow')
        result |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return result, pos
    raise ValueError('protobuf varint overflow')


def decode_proto(buf: bytes) -> list[tuple[int, str, int | bytes]]:
    if len(buf) > 64 * 1024 * 1024:
        raise ValueError('protobuf payload too large')
    pos, fields = 0, []
    while pos < len(buf):
        tag, pos = decode_varint(buf, pos)
        fnum, wire = tag >> 3, tag & 7
        if not 0 < fnum <= 0x1fffffff:
            raise ValueError('invalid protobuf field')
        if wire == 0:
            value, pos = decode_varint(buf, pos)
            fields.append((fnum, 'varint', value))
        elif wire == 2:
            length, pos = decode_varint(buf, pos)
            if length > len(buf) - pos:
                raise ValueError('truncated protobuf bytes')
            fields.append((fnum, 'bytes', buf[pos:pos + length]))
            pos += length
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if size > len(buf) - pos:
                raise ValueError('truncated protobuf fixed field')
            pos += size
        else:
            raise ValueError('unsupported protobuf wire type')
    return fields


def parse_proto_duration(sub: bytes) -> float:
    if not sub:
        return 0.0
    f = {k: v for k, t, v in decode_proto(sub) if t == "varint" and isinstance(v, int)}
    return float(f.get(1, 0)) + float(f.get(2, 0)) / 1e9


def parse_annotation_text(text: str) -> dict:
    """Read Protobuf text with nested messages, lists, comments and C escapes.

    A stdlib parser keeps the sidecar dependency-free. Unknown fields are parsed,
    so quoted field names cannot masquerade as archive flags. Malformed data is
    rejected rather than used to authorize mutations.
    """
    if len(text) > 1024 * 1024:
        raise ValueError('annotations too large')
    lexer = re.compile(r"""\s+|\#[^\n]*|//[^\n]*|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|[{}<>:\[\],;]|[^\s{}<>:\[\],;"'\#]+""")
    tokens, pos = [], 0
    for match in lexer.finditer(text):
        if match.start() != pos:
            raise ValueError('invalid annotations token')
        pos = match.end()
        token = match.group()
        if not token.isspace() and not token.startswith(('#', '//')):
            tokens.append(token)
    if pos != len(text):
        raise ValueError('unterminated annotations string')

    def unescape(token):
        out, i, body = bytearray(), 0, token[1:-1]
        escapes = {'a': 7, 'b': 8, 'f': 12, 'n': 10, 'r': 13,
                   't': 9, 'v': 11, '\\': 92, '"': 34, "'": 39, '?': 63}
        while i < len(body):
            ch = body[i]
            i += 1
            if ch != '\\':
                out.extend(ch.encode('utf-8'))
                continue
            if i == len(body):
                raise ValueError('incomplete annotations escape')
            ch = body[i]
            i += 1
            if ch in escapes:
                out.append(escapes[ch])
            elif ch in '01234567':
                digits = ch
                while i < len(body) and len(digits) < 3 and body[i] in '01234567':
                    digits += body[i]
                    i += 1
                out.append(int(digits, 8))
            elif ch in 'xuU':
                limit = {'x': 2, 'u': 4, 'U': 8}[ch]
                digits = ''
                while i < len(body) and len(digits) < limit and body[i] in '0123456789abcdefABCDEF':
                    digits += body[i]
                    i += 1
                if not digits or (ch != 'x' and len(digits) != limit):
                    raise ValueError('invalid annotations hex escape')
                number = int(digits, 16)
                out.extend(bytes([number]) if ch == 'x' else chr(number).encode('utf-8'))
            else:
                raise ValueError('unknown annotations escape')
        return out

    index = 0

    def value(depth):
        nonlocal index
        if index >= len(tokens) or depth > 32:
            raise ValueError('invalid annotations value')
        token = tokens[index]
        index += 1
        if token in ('{', '<'):
            return message('}' if token == '{' else '>', depth + 1)
        if token == '[':
            result = []
            while index < len(tokens) and tokens[index] != ']':
                result.append(value(depth + 1))
                if index < len(tokens) and tokens[index] == ',':
                    index += 1
                elif index < len(tokens) and tokens[index] != ']':
                    raise ValueError('invalid annotations list')
            if index == len(tokens):
                raise ValueError('unterminated annotations list')
            index += 1
            return result
        if token.startswith(('"', "'")):
            result = unescape(token)
            while index < len(tokens) and tokens[index].startswith(('"', "'")):
                result.extend(unescape(tokens[index]))
                index += 1
            return result.decode('utf-8')
        if token in ('true', 'false', 'True', 'False'):
            return token.lower() == 'true'
        try:
            return int(token, 10) if re.fullmatch(r'[+-]?\d+', token) else int(token, 0)
        except ValueError:
            if re.fullmatch(r'[A-Za-z_][\w.]*', token):
                return token
            raise ValueError('invalid annotations scalar') from None

    def message(end='', depth=0):
        nonlocal index
        result = {}
        while index < len(tokens) and tokens[index] != end:
            key = tokens[index]
            index += 1
            if not re.fullmatch(r'[A-Za-z_][\w.]*', key):
                raise ValueError('invalid annotations field')
            if index < len(tokens) and tokens[index] == ':':
                index += 1
            elif index == len(tokens) or tokens[index] not in ('{', '<'):
                raise ValueError('missing annotations colon')
            item = value(depth)
            if key in result and key in ('title', 'archived', 'last_user_view_time'):
                raise ValueError('duplicate annotation field')
            result[key] = item
            if index < len(tokens) and tokens[index] in (',', ';'):
                index += 1
        if end:
            if index == len(tokens):
                raise ValueError('unterminated annotations message')
            index += 1
        return result

    result = message()
    if 'title' in result and not isinstance(result['title'], str):
        raise ValueError('invalid annotation title')
    if 'archived' in result and (type(result['archived']) not in (bool, int) or result['archived'] not in (0, 1)):
        raise ValueError('invalid archived flag')
    result['archived'] = bool(result.get('archived', False))
    return result
