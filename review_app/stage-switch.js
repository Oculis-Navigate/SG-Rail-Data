function initStageSwitch(current) {
  const control = document.getElementById('stageSwitch');
  const slider = control.querySelector('input');
  const render = () => {
    control.dataset.stage = slider.value;
    slider.setAttribute('aria-valuetext', slider.value === '2' ? 'Stage 2: exit coordinates' : 'Stage 3: track geometry');
  };
  const reset = () => { slider.value = String(current); render(); };
  reset();
  window.addEventListener('pageshow', reset);
  slider.addEventListener('input', render);
  slider.addEventListener('change', () => {
    if (Number(slider.value) !== current) location.assign(slider.value === '2' ? '/' : '/stage3/');
  });
}
