const fs = require('fs');
const { PDFDocument } = require('pdf-lib');
(async () => {
  const p = 'C:/Users/Damjan/Downloads/itinerary (11).pdf';
  const b = fs.readFileSync(p);
  const pdf = await PDFDocument.load(b);
  console.log('pages', pdf.getPageCount());
})();
