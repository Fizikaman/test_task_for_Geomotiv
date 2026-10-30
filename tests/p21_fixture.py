"""Tiny vector-only relationship fixture; names have no textual relation."""


def diagram_pdf(*, arrow: bool = True) -> bytes:
    content = b"BT /F1 9 Tf 5 70 Td (Alice) Tj 65 0 Td (Bob) Tj ET"
    if arrow:
        content += b" 1 0 0 RG 3 w 10 20 m 90 20 l S 80 15 m 90 20 l 80 25 l S"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [5 0 R] >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>",
    ]
    data = bytearray(b"%PDF-1.7\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return bytes(data)
