// frontend/persona.js — capture persona answers and attach to /invoke payloads
const LS_KEY = "daisy.persona";
let personaState = null;

export function initPersona() {
  try {
    personaState = JSON.parse(window.localStorage.getItem(LS_KEY) || "null");
  } catch {
    personaState = null;
  }
  return personaState;
}

export function setPersonaFromQuestionnaire(answer) {
  const txt = (answer || "").toString().trim().toLowerCase();
  let persona = null;
  if (txt.startsWith("1") || txt.includes("analytical")) persona = "ANALYTICAL_CURATOR";
  else if (txt.startsWith("2") || txt.includes("rational")) persona = "RATIONAL_EXPLORER";
  else if (txt.startsWith("3") || txt.includes("sentimental")) persona = "SENTIMENTAL_VOYAGER";
  else if (txt.startsWith("4") || txt.includes("experiential")) persona = "EXPERIENTIAL_LIBERTINE";
  if (persona) {
    personaState = { personaState: persona };
    try { window.localStorage.setItem(LS_KEY, JSON.stringify(personaState)); } catch {}
  }
  return personaState;
}

export function clearPersona() {
  personaState = null;
  try { window.localStorage.removeItem(LS_KEY); } catch {}
}

export function attachPersona(body = {}) {
  if (!personaState) return { ...body };
  return { ...body, persona: personaState };
}
