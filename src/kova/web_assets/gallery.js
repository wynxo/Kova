"use strict";
document.querySelectorAll("[data-theme-value]").forEach(button => {
  button.addEventListener("click", () => {
    document.documentElement.dataset.theme = button.dataset.themeValue;
    document.querySelectorAll("[data-theme-value]").forEach(other => {
      other.setAttribute("aria-pressed", String(other === button));
    });
  });
});
document.querySelectorAll("[data-density-value]").forEach(button => {
  button.addEventListener("click", () => {
    document.documentElement.dataset.density = button.dataset.densityValue;
    document.querySelectorAll("[data-density-value]").forEach(other => {
      other.setAttribute("aria-pressed", String(other === button));
    });
  });
});
document.querySelectorAll("[data-motion-value]").forEach(button => {
  button.addEventListener("click", () => {
    if (button.dataset.motionValue === "reduced") {
      document.documentElement.dataset.motion = "reduced";
    } else {
      delete document.documentElement.dataset.motion;
    }
    document.querySelectorAll("[data-motion-value]").forEach(other => {
      other.setAttribute("aria-pressed", String(other === button));
    });
  });
});
