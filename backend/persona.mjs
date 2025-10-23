// backend/persona.mjs — ultra-light persona classifier for sessions
export function normalize(value) {
  return (value || "").toString().trim().toLowerCase();
}

export function chooseFromQuestionnaire(answer) {
  const s = normalize(answer);
  if (/^1\b|analytical|curator/.test(s)) return "ANALYTICAL_CURATOR";
  if (/^2\b|rational|explorer/.test(s)) return "RATIONAL_EXPLORER";
  if (/^3\b|sentimental|voyager/.test(s)) return "SENTIMENTAL_VOYAGER";
  if (/^4\b|experiential|libertine/.test(s)) return "EXPERIENTIAL_LIBERTINE";
  return null;
}

export function updatePersona(prev = {}, userUtterance = "") {
  const persona = { ...prev };
  const s = normalize(userUtterance);
  const questionnaire = chooseFromQuestionnaire(s);
  if (questionnaire) persona.personaState = questionnaire;

  if (/\b(budget|cheap|lowest|economy|basic)\b/.test(s)) persona.budget = "budget";
  if (/\b(luxury|first class|business|premium)\b/.test(s)) persona.budget = "premium";
  if (/\b(direct only|nonstop only|hate layovers|no stops)\b/.test(s)) persona.layovers = "avoid";
  if (/\b(ok with layovers|fine with stop|2 stops|multi stop)\b/.test(s)) persona.layovers = "allow";
  if (/\bwith kids|family|toddler|children\b/.test(s)) persona.context = "family";
  if (/\bhoneymoon|anniversary|proposal|romance\b/.test(s)) persona.context = "romance";
  if (/\b(mountain|alps|ski|hike|trek)\b/.test(s)) persona.activities = "outdoor";
  if (/\b(beach|surf|dive|snorkel)\b/.test(s)) persona.activities = "beach";
  if (/\b(museum|gallery|opera|theatre|food|wine)\b/.test(s)) persona.activities = "culture";

  if (!persona.personaState) {
    if (/\bcompare|optimal|best option|details\b/.test(s)) persona.personaState = "ANALYTICAL_CURATOR";
    else if (/\bflexible|decide later|open to\b/.test(s)) persona.personaState = "RATIONAL_EXPLORER";
    else if (/\bmeaningful|special|memories|story\b/.test(s)) persona.personaState = "SENTIMENTAL_VOYAGER";
    else if (/\bsurprise me|adventure|spontaneous\b/.test(s)) persona.personaState = "EXPERIENTIAL_LIBERTINE";
  }
  return persona;
}

export function toSessionAttributes(persona = {}) {
  return {
    personaState: persona.personaState || "RATIONAL_EXPLORER",
    budget: persona.budget || null,
    layovers: persona.layovers || null,
    context: persona.context || null,
    activities: persona.activities || null,
  };
}
