const fs = require('fs');
const { PDFDocument, StandardFonts } = require('pdf-lib');

(async () => {
  const path = 'frontend/img/lufthansa_template_empty_form.pdf';
  const bytes = fs.readFileSync(path);
  const src = await PDFDocument.load(bytes);
  const form = src.getForm();
  const set = (n,v) => { try { form.getTextField(n).setText(String(v||'')); } catch (e) { console.log('no field', n); } };
  set('from_to', 'FRA -> LHR');
  set('passenger_left', 'Happy User');
  set('flight_left', 'LH 1234');
  set('date_left', '2025-10-20');
  set('departure_left', '08:00');
  set('seq_no_left', '07-11');
  set('gate_left', 'A12');
  set('zone_left', '2');
  set('from_left', 'FRA');
  set('to_left', 'LHR');
  set('passenger_right', 'Happy User');
  set('date_right', '2025-10-20');
  set('departure_right', '08:00');
  set('gate_right', 'A12');
  set('zone_right', '2');
  set('seat_right', '12A');
  set('seq_no_right', '07-11');
  set('flight_right', 'LH 1234');
  try { const helv = await src.embedFont(StandardFonts.Helvetica); form.updateFieldAppearances(helv); } catch(e) { try { form.updateFieldAppearances(); } catch(_){} }
  try { form.flatten(); } catch(_){}
  const out = await src.save();
  fs.writeFileSync('tmp_filled.pdf', out);
  console.log('wrote tmp_filled.pdf', out.length);
})();

