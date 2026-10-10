const fs = require('node:fs');
const vm = require('node:vm');

const markup = fs.readFileSync(process.argv[2], 'utf8');
const payload = markup.match(/<script type="application\/json" id="story-data">([\s\S]*?)<\/script>/)[1];
const script = markup.match(/<script>\s*([\s\S]*?)<\/script>/)[1]
  .replace('window.ARTFOLIO_EDITOR=null;', 'window.ARTFOLIO_EDITOR={token:"test-session"};');
const plan = JSON.parse(payload).plan;
let onSave;
let request;
let reloaded = false;
const button = { disabled: true, addEventListener: (_, callback) => { onSave = callback; } };
const status = { textContent: '' };
const articles = plan.slides.map((slide, index) => ({
  dataset: { slide: slide.id },
  querySelector: selector => ({
    value: selector === '.title' ? slide.title : selector === '.body' ? slide.body : String(index + 1),
  }),
  querySelectorAll: () => [process.argv[3], '0.12', '0.91', '0.6'].map(value => ({ value })),
}));
const elements = {
  'story-data': { textContent: payload },
  save: button,
  status,
  'public-title': { value: plan.public_title },
  'cover-style': { value: plan.cover_style },
};

vm.runInNewContext(script, {
  document: { getElementById: id => elements[id], querySelectorAll: () => articles },
  window: {},
  structuredClone,
  location: { reload: () => { reloaded = true; } },
  fetch: async (_, options) => {
    request = JSON.parse(options.body);
    return { ok: true, json: async () => ({ revision: 2 }) };
  },
});

onSave().then(() => {
  process.stdout.write(JSON.stringify({
    saved: Boolean(request),
    focus: request?.plan.slides.find(slide => slide.role === 'detail').focus,
    reloaded,
    status: status.textContent,
    disabled: button.disabled,
  }));
}).catch(error => { console.error(error); process.exitCode = 1; });
