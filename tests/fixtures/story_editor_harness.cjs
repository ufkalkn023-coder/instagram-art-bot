const fs = require('node:fs');
const vm = require('node:vm');

const markup = fs.readFileSync(process.argv[2], 'utf8');
const payload = markup.match(/<script type="application\/json" id="story-data">([\s\S]*?)<\/script>/)[1];
const script = markup.match(/<script>\s*([\s\S]*?)<\/script>/)[1]
  .replace('window.ARTFOLIO_EDITOR=null;', 'window.ARTFOLIO_EDITOR={token:"test-session"};');
const plan = JSON.parse(payload).plan;
let request;
let reloaded = false;
function element(properties = {}) {
  const listeners = new Map();
  return {
    hidden: false, style: {},
    addEventListener: (type, callback) => listeners.set(type, callback),
    emit: (type, event = {}) => listeners.get(type)?.(event),
    ...properties,
  };
}
const button = element({ disabled: true });
const status = { textContent: '' };
let previewCrop;
const surface = element({
  getBoundingClientRect: () => ({ left: 100, top: 200, width: 300, height: 200 }),
  setPointerCapture: () => {}, releasePointerCapture: () => {}, hasPointerCapture: () => true,
});
const image = element({ complete: true, naturalWidth: 600, naturalHeight: 400 });
const selection = element();
const preview = element({ getContext: () => ({
  clearRect: () => {},
  drawImage: (_, x, y, width, height) => { previewCrop = [x, y, width, height]; },
}) });
const feedback = element();
const coordinates = [process.argv[3], '0.12', '0.91', '0.6'].map(value => element({ value }));
const articles = plan.slides.map((slide, index) => {
  const controls = {
    '.title': { value: slide.title }, '.body': { value: slide.body }, '.order': { value: String(index + 1) },
    '.crop-surface': surface, '.crop-original': image, '.crop-selection': selection,
    '.crop-preview': preview, '.crop-feedback': feedback,
  };
  return {
    dataset: { slide: slide.id },
    querySelector: selector => controls[selector],
    querySelectorAll: () => slide.focus ? coordinates : [],
  };
});
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

const scenario = process.argv[4];
const pointer = (x, y) => ({ clientX: x, clientY: y, pointerId: 7, button: 0, isPrimary: true, preventDefault: () => {} });
if (scenario) {
  const start = scenario === 'reverse' ? [340, 360] : scenario === 'clamp' ? [100, 200] : [130, 240];
  const end = scenario === 'reverse' ? [130, 240] : scenario === 'clamp' ? [450, 500] : scenario === 'zero' ? start : [340, 360];
  surface.emit('pointerdown', pointer(...start));
  surface.emit('pointermove', pointer(...end));
  surface.emit(scenario === 'cancel' ? 'pointercancel' : 'pointerup', pointer(...end));
}
button.emit('click').then(() => {
  process.stdout.write(JSON.stringify({
    saved: Boolean(request),
    focus: request?.plan.slides.find(slide => slide.role === 'detail').focus,
    reloaded,
    status: status.textContent,
    disabled: button.disabled,
    revision: request?.expected_revision,
    preview_hidden: preview.hidden,
    preview_crop: previewCrop,
  }));
}).catch(error => { console.error(error); process.exitCode = 1; });
