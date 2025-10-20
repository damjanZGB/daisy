const fs = require('fs');
const { PDFDocument } = require('pdf-lib');

(async () => {
  const path = 'frontend/img/lufthansa_template_empty_form.pdf';
  const bytes = fs.readFileSync(path);
  const src = await PDFDocument.load(bytes);
  const form = src.getForm ? src.getForm() : null;
  const fields = form ? form.getFields().map(f => f.getName()) : [];
  const pages = src.getPageCount();
  console.log('pages:', pages, 'fields:', fields.length, fields);

  if (form) {
    try { form.updateFieldAppearances(); } catch (e) { console.log('updateFieldAppearances error:', e && e.message); }
    try { form.flatten(); } catch (e) { console.log('flatten error:', e && e.message); }
  }

  const dest = await PDFDocument.create();
  const copied = await dest.copyPages(src, [0]);
  dest.addPage(copied[0]);
  const out = await dest.save();
  fs.writeFileSync('tmp_copied.pdf', out);
  console.log('wrote tmp_copied.pdf', out.length);
})();

