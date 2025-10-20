#!/usr/bin/env node
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { PDFDocument } from 'pdf-lib';

const FORM_PATH = process.argv[2] || join(process.cwd(), 'frontend', 'img', 'lufthansa_template_empty_form.pdf');

async function main() {
  const bytes = readFileSync(FORM_PATH);
  const pdf = await PDFDocument.load(bytes);
  const form = pdf.getForm();
  const fields = form.getFields().map(f => ({ name: f.getName(), type: f.constructor.name }));
  console.log(JSON.stringify({ path: FORM_PATH, fieldCount: fields.length, fields }, null, 2));
}

main().catch(e => { console.error('Failed to read form:', e); process.exit(1); });

