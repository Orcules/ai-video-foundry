/* VidBuddy — effects.js
   Scroll reveal · ripple · header shadow · gallery stagger
   ============================================================ */
(function () {
  'use strict';

  /* ── Intersection Observer for .vb-reveal elements ─────────────── */
  var io = null;
  if ('IntersectionObserver' in window) {
    io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.1, rootMargin: '0px 0px -28px 0px' });
  }

  function observeReveal(root) {
    if (!io) return;
    root.querySelectorAll('.vb-reveal:not(.is-visible)').forEach(function (el) {
      io.observe(el);
    });
  }

  /* ── Header scroll shadow ──────────────────────────────────────── */
  var header = document.querySelector('.studio-header');
  if (header) {
    window.addEventListener('scroll', function () {
      header.classList.toggle('is-scrolled', window.scrollY > 12);
    }, { passive: true });
  }

  /* ── Gallery card stagger index ────────────────────────────────── */
  function indexGalleryCards() {
    var grid = document.getElementById('studioGalleryGrid');
    if (!grid) return;
    grid.querySelectorAll('.studio-gallery-card').forEach(function (card, i) {
      card.style.setProperty('--vb-i', i);
    });
  }

  /* ── Button ripple ─────────────────────────────────────────────── */
  document.addEventListener('pointerdown', function (e) {
    var btn = e.target.closest('.studio-btn-primary, .studio-btn-primary-lg');
    if (!btn) return;
    var rect = btn.getBoundingClientRect();
    var size = Math.max(rect.width, rect.height) * 2.4;
    var ripple = document.createElement('span');
    ripple.style.cssText =
      'position:absolute;pointer-events:none;border-radius:50%;' +
      'background:rgba(255,255,255,0.25);' +
      'width:' + size + 'px;height:' + size + 'px;' +
      'top:' + (e.clientY - rect.top - size / 2) + 'px;' +
      'left:' + (e.clientX - rect.left - size / 2) + 'px;' +
      'transform:scale(0);animation:vbRipple 0.55s ease-out forwards;';
    btn.appendChild(ripple);
    ripple.addEventListener('animationend', function () { ripple.remove(); }, { once: true });
  });

  /* ── Step activation observer ──────────────────────────────────── */
  /* Re-runs scroll reveal when a new step becomes active */
  if ('MutationObserver' in window) {
    var stepObs = new MutationObserver(function (mutations) {
      mutations.forEach(function (m) {
        if (m.type === 'attributes' &&
            m.attributeName === 'class' &&
            m.target.classList.contains('studio-step') &&
            m.target.classList.contains('active')) {
          // Give the step animation a head start, then observe reveals inside
          setTimeout(function () { observeReveal(m.target); }, 200);
        }
      });
    });

    document.querySelectorAll('.studio-step').forEach(function (step) {
      stepObs.observe(step, { attributes: true, attributeFilter: ['class'] });
    });

    /* Watch gallery grid for newly added cards */
    var galleryGrid = document.getElementById('studioGalleryGrid');
    if (galleryGrid) {
      var galleryObs = new MutationObserver(function () {
        requestAnimationFrame(function () {
          indexGalleryCards();
          observeReveal(galleryGrid);
        });
      });
      galleryObs.observe(galleryGrid, { childList: true });
    }
  }

  /* ── Smooth scroll to top when navigating between steps ────────── */
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-next], [data-prev]');
    if (btn) {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }, { capture: false });

  /* ── Parallax blobs on mouse move ──────────────────────────────── */
  (function () {
    var ticking = false;
    document.addEventListener('mousemove', function (e) {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(function () {
        var xPct = (e.clientX / window.innerWidth - 0.5) * 2;   /* -1 to 1 */
        var yPct = (e.clientY / window.innerHeight - 0.5) * 2;
        document.documentElement.style.setProperty('--blob-x', (xPct * 18) + 'px');
        document.documentElement.style.setProperty('--blob-y', (yPct * 14) + 'px');
        ticking = false;
      });
    }, { passive: true });
  })();

  /* ── Initial pass ──────────────────────────────────────────────── */
  function init() {
    observeReveal(document);
    indexGalleryCards();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
