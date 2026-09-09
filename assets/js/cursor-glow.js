/**
 * Effet de lumière qui suit le curseur.
 * À inclure juste avant la fermeture de </body>, après avoir ajouté
 * <div id="cursor-glow"></div> juste après la balise <body>.
 */
(function () {
  var glow = document.getElementById('cursor-glow');
  if (!glow) return;

  // Ne rien faire sur mobile/tactile (pas de curseur)
  if (window.matchMedia('(hover: none), (pointer: coarse)').matches) return;

  var mouseX = window.innerWidth / 2;
  var mouseY = window.innerHeight * 0.4;
  var ticking = false;

  function updateGlow() {
    glow.style.setProperty('--x', mouseX + 'px');
    glow.style.setProperty('--y', mouseY + 'px');
    ticking = false;
  }

  window.addEventListener('mousemove', function (e) {
    mouseX = e.clientX;
    mouseY = e.clientY;
    if (!ticking) {
      requestAnimationFrame(updateGlow);
      ticking = true;
    }
  });
})();
