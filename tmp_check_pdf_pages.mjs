import { readFileSync } from 'node:fs';
import { PDFDocument } from 'pdf-lib';
const p = 'C:/Users/Damjan/Downloads/itinerary (11).pdf';
const b = readFileSync(p);
const pdf = await PDFDocument.load(b);
console.log('pages', pdf.getPageCount());
