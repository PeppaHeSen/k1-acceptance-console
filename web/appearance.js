"use strict";

const consoleAppearance = (() => {
  const system = matchMedia("(prefers-color-scheme: dark)");
  const storageKeys = {theme:"k1-theme", transparency:"k1-glass-transparency"};
  let theme = "system";
  let transparency = 36;
  try {
    const savedTheme = localStorage.getItem(storageKeys.theme);
    if (["system","light","dark"].includes(savedTheme)) theme = savedTheme;
    const saved = localStorage.getItem(storageKeys.transparency);
    if (saved !== null && /^\d{1,2}$/.test(saved) && Number(saved)<=60) transparency = Number(saved);
  } catch {}

  function apply() {
    document.documentElement.dataset.resolvedTheme = theme==="system" ? (system.matches ? "dark" : "light") : theme;
    document.documentElement.style.setProperty("--glass-opacity",(100-transparency)+"%");
  }
  system.addEventListener("change",apply);
  apply();
  return {
    get theme() { return theme; },
    get transparency() { return transparency; },
    update(mode, value) {
      theme = mode;
      transparency = value;
      try {
        localStorage.setItem(storageKeys.theme,theme);
        localStorage.setItem(storageKeys.transparency,transparency);
      } catch {}
      apply();
    }
  };
})();
