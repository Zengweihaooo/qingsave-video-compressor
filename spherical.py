"""Inject Google Spherical Video V1 metadata into a tail-moov MP4.

Only full 2:1, mono, already stitched equirectangular video is supported.
mdat remains in place, so sample offsets never change.
"""
import struct
import uuid

SPHERICAL = uuid.UUID('ffcc8263-f855-4a93-8814-587a02521fdd').bytes

def boxes(data):
    pos = 0
    while pos < len(data):
        if len(data)-pos < 8: raise ValueError('Invalid MP4 box')
        size, kind = struct.unpack_from('>I4s', data, pos)
        if size < 8 or pos+size > len(data): raise ValueError('Unsupported MP4 box size')
        yield kind, data[pos+8:pos+size]
        pos += size

def box(kind, body):
    return struct.pack('>I4s', len(body)+8, kind) + body

def inject(path):
    xml = ('<rdf:SphericalVideo xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
           'xmlns:GSpherical="http://ns.google.com/videos/1.0/spherical/">'
           '<GSpherical:Spherical>true</GSpherical:Spherical>'
           '<GSpherical:Stitched>true</GSpherical:Stitched>'
           '<GSpherical:StitchingSoftware>QingSave panorama pipeline</GSpherical:StitchingSoftware>'
           '<GSpherical:ProjectionType>equirectangular</GSpherical:ProjectionType>'
           '<GSpherical:StereoMode>mono</GSpherical:StereoMode></rdf:SphericalVideo>').encode()
    with open(path, 'r+b') as f:
        f.seek(0, 2); length = f.tell(); f.seek(0)
        while f.tell() < length:
            offset = f.tell()
            size, kind = struct.unpack('>I4s', f.read(8)); header = 8
            if size == 1: size = struct.unpack('>Q', f.read(8))[0]; header = 16
            if size < header or offset+size > length: raise ValueError('Invalid MP4')
            if kind != b'moov': f.seek(offset+size); continue
            if offset+size != length or header != 8: raise ValueError('360 injection requires tail moov')
            data = f.read(size-header); result = b''; count = 0
            for k, body in boxes(data):
                if k == b'trak':
                    is_video = any(mk == b'mdia' and any(hk == b'hdlr' and hb[8:12] == b'vide' for hk, hb in boxes(mb)) for mk, mb in boxes(body))
                    if is_video:
                        body = b''.join(box(ck, cb) for ck, cb in boxes(body) if not (ck == b'uuid' and cb.startswith(SPHERICAL)))
                        body += box(b'uuid', SPHERICAL+xml); count += 1
                result += box(k, body)
            if count != 1: raise ValueError('Expected one video track')
            f.seek(offset); f.write(box(b'moov', result)); f.truncate(); return
    raise ValueError('Missing moov')
