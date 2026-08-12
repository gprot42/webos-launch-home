import qrcode from 'qrcode-generator';

/**
 * SVG markup for a high-contrast QR code (phone-scan from a sofa).
 */
export function qrSvgMarkup(text) {
  const data = String(text || '').trim();
  if (!data) return '';
  const qr = qrcode(0, 'M');
  qr.addData(data, 'Byte');
  qr.make();
  return qr.createSvgTag({cellSize: 8, margin: 2, scalable: true});
}
