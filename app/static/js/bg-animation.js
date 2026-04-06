/**
 * bg-animation.js — Live trading background
 * Animated price-chart canvas + orbiting soft glows.
 * Auto-loaded by icons.js on every page.
 */
(function () {
  'use strict';

  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  // ── Orb CSS ──────────────────────────────────────────────────────────────────
  var orbCss = [
    '.bg-orb{position:fixed;border-radius:50%;pointer-events:none;z-index:-1;will-change:transform;}',
    '@keyframes _o1{0%,100%{transform:translate(0,0) scale(1)}   50%{transform:translate(13vw,-9vh)  scale(1.07)}}',
    '@keyframes _o2{0%,100%{transform:translate(0,0) scale(1)}   50%{transform:translate(-16vw,13vh) scale(.93)}}',
    '@keyframes _o3{0%,100%{transform:translate(0,0)}33%{transform:translate(9vw,16vh)}66%{transform:translate(-7vw,7vh)}}',
    '@keyframes _o4{0%,100%{transform:translate(0,0)}            50%{transform:translate(-13vw,-17vh)}}',
    '@keyframes _o5{0%,100%{transform:translate(0,0) scale(1)}   45%{transform:translate(17vw,9vh) scale(1.1)}}',
  ].join('');
  var st = document.createElement('style');
  st.textContent = orbCss;
  document.head.appendChild(st);

  // [left%, top%, diameter_px, rgb, opacity, duration_s, delay_s]
  var ORB_DEFS = [
    [  4,  6, 680, '99,102,241',  .22, 30,   0],
    [ 72, 56, 560, '139,92,246',  .16, 40,  -7],
    [ 44, 16, 460, '59,130,246',  .14, 24, -13],
    [ 16, 70, 380, '168,85,247',  .12, 34, -10],
    [ 84,  2, 320, '20,184,166',  .10, 21,  -4],
  ];

  function injectOrbs() {
    ORB_DEFS.forEach(function (o, i) {
      var d = document.createElement('div');
      d.className = 'bg-orb';
      d.style.cssText =
        'left:'   + o[0] + '%;top:'    + o[1] + '%;' +
        'width:'  + o[2] + 'px;height:' + o[2] + 'px;' +
        'background:radial-gradient(circle,' +
          'rgba(' + o[3] + ',' + o[4] + ') 0%,' +
          'rgba(' + o[3] + ',0) 70%);' +
        'animation:_o' + (i + 1) + ' ' + o[5] + 's ease-in-out ' + o[6] + 's infinite;';
      document.body.appendChild(d);
    });
  }

  // ── Canvas price-chart animation ─────────────────────────────────────────────
  var SERIES = [
    { c: '99,102,241',  lop: .30, fop: .10, lw: 1.6, drift: -.49  },
    { c: '139,92,246',  lop: .18, fop: .06, lw: 1.1, drift: -.488 },
    { c: '59,130,246',  lop: .12, fop: .04, lw: .8, drift: -.492 },
  ];
  var XSTEP      = 3;   // px per data point
  var SKIP       = 9;   // frames between new price points (slower = calmer)
  var MAX_SWING  = 22;  // max random price move per tick

  var canvas, ctx, W, H, frameN = 0, paused = false;

  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  function initSeries() {
    var n = Math.ceil(W / XSTEP) + 4;
    SERIES.forEach(function (s, si) {
      s.p = H * (0.38 + si * 0.11);
      s.pts = [];
      for (var k = 0; k < n; k++) {
        s.pts.push(s.p);
        s.p = clamp(s.p + (Math.random() + s.drift) * MAX_SWING * 1.2,
                    H * 0.07, H * 0.93);
      }
    });
  }

  function resize() {
    W = canvas.width  = window.innerWidth;
    H = canvas.height = window.innerHeight;
    initSeries();
  }

  function drawSeries(s) {
    var pts = s.pts, n = pts.length;
    if (n < 2) return;

    // Filled area under curve
    ctx.beginPath();
    ctx.moveTo(0, pts[0]);
    for (var i = 1; i < n; i++) {
      var cpx = ((i - 1) * XSTEP + i * XSTEP) * 0.5;
      ctx.bezierCurveTo(cpx, pts[i - 1], cpx, pts[i], i * XSTEP, pts[i]);
    }
    ctx.lineTo((n - 1) * XSTEP, H + 20);
    ctx.lineTo(0, H + 20);
    ctx.closePath();
    var grd = ctx.createLinearGradient(0, 0, 0, H);
    grd.addColorStop(0, 'rgba(' + s.c + ',' + s.fop + ')');
    grd.addColorStop(1, 'rgba(' + s.c + ',0)');
    ctx.fillStyle = grd;
    ctx.fill();

    // Line
    ctx.beginPath();
    ctx.moveTo(0, pts[0]);
    for (var j = 1; j < n; j++) {
      var cpx2 = ((j - 1) * XSTEP + j * XSTEP) * 0.5;
      ctx.bezierCurveTo(cpx2, pts[j - 1], cpx2, pts[j], j * XSTEP, pts[j]);
    }
    ctx.strokeStyle = 'rgba(' + s.c + ',' + s.lop + ')';
    ctx.lineWidth   = s.lw;
    ctx.lineJoin    = 'round';
    ctx.stroke();
  }

  function tick() {
    if (paused) return;
    frameN++;

    if (frameN % SKIP === 0) {
      var maxLen = Math.ceil(W / XSTEP) + 4;
      SERIES.forEach(function (s) {
        s.p = clamp(s.p + (Math.random() + s.drift) * MAX_SWING,
                    H * 0.07, H * 0.93);
        s.pts.push(s.p);
        if (s.pts.length > maxLen) s.pts.shift();
      });
    }

    ctx.clearRect(0, 0, W, H);

    // Subtle top-center indigo glow (drawn on canvas so it pulses slightly with the chart)
    var glowGrd = ctx.createRadialGradient(W * 0.5, 0, 0, W * 0.5, 0, W * 0.55);
    glowGrd.addColorStop(0, 'rgba(99,102,241,.14)');
    glowGrd.addColorStop(1, 'rgba(99,102,241,0)');
    ctx.fillStyle = glowGrd;
    ctx.fillRect(0, 0, W, H * 0.55);

    SERIES.forEach(drawSeries);

    requestAnimationFrame(tick);
  }

  function init() {
    canvas = document.createElement('canvas');
    canvas.style.cssText = 'position:fixed;inset:0;z-index:-1;pointer-events:none;';
    document.body.insertBefore(canvas, document.body.firstChild);
    ctx = canvas.getContext('2d');

    injectOrbs();
    resize();
    window.addEventListener('resize', resize);

    // Pause when tab is hidden (save CPU/GPU)
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        paused = true;
      } else {
        paused = false;
        requestAnimationFrame(tick);
      }
    });

    requestAnimationFrame(tick);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
