// The 3D view: shows the STL files of a build, with dimensions drawn around
// the model, and the scans it is compared with drawn over it. Z is up, the
// camera is orthographic (lengths read true), and the scene is rendered only
// when something changed.
//
// Dimensions re-anchor as the camera moves: each overall dimension sits on the
// side of the bounding box that faces the viewer, offset by a constant distance
// on screen, and hides when its axis points into the screen.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { CSS2DObject, CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js';
import { toCreasedNormals } from 'three/addons/utils/BufferGeometryUtils.js';

import { locale, t } from './i18n.js';

THREE.Object3D.DEFAULT_UP.set(0, 0, 1);

const SVG_NS = 'http://www.w3.org/2000/svg';
const FRUSTUM = 100;          // world units across the view height at zoom 1
const CREASE = 0.55;          // radians: facets flatter than this shade as one smooth surface
const EDGE_ANGLE = 28;        // degrees: sharper than this is drawn as an edge
const HEAVY_TRIANGLES = 200_000; // beyond this, skip smoothing and edges
const GEOMETRY_CACHE = 24;
const SCAN_CACHE = 6;
const GAP_PX = 30;            // distance of a dimension line from the model, on screen
const TICK_PX = 5;
const OVERSHOOT_PX = 7;
const ROW_PX = 21;            // height of a label plus the gap to the next one
const FILL = 0.5;             // share of the free area a framed model takes up
const MOVE_MS = 280;          // a change of view glides for this long
const SETTLE_MS = 700;        // after a drag, the camera coasts to a stop for this long

// The ground: lines at every 1, 10, 100... mm, each level fading in as its
// cells grow on screen, and the whole thing fading out away from the model.
const GRID_VERTEX = `
  varying vec2 vWorld;
  void main() {
    vec4 world = modelMatrix * vec4(position, 1.0);
    vWorld = world.xy;
    gl_Position = projectionMatrix * viewMatrix * world;
  }`;
const GRID_FRAGMENT = `
  uniform vec2 uCenter;
  uniform float uRadius;
  uniform float uPixel;   // world units per screen pixel
  uniform vec3 uColor;
  varying vec2 vWorld;

  float level(float cell) {
    vec2 coord = vWorld / cell;
    vec2 near = abs(fract(coord - 0.5) - 0.5) / fwidth(coord);
    float line = 1.0 - min(min(near.x, near.y), 1.0);
    float px = cell / uPixel; // one cell on screen
    return line * smoothstep(5.0, 40.0, px) * mix(0.3, 1.0, smoothstep(40.0, 600.0, px));
  }

  void main() {
    float finest = pow(10.0, floor(log(uPixel * 5.0) / log(10.0)));
    float lines = max(level(finest), max(level(finest * 10.0), level(finest * 100.0)));
    float fade = 1.0 - smoothstep(0.1, 1.0, distance(vWorld, uCenter) / uRadius);
    float alpha = lines * fade * fade * 0.42;
    if (alpha < 0.004) discard;
    gl_FragColor = vec4(uColor, alpha);
  }`;

const VIEWS = {
  iso: [1, -1, 0.8],
  top: [0, -0.0001, 1],
  front: [0, -1, 0],
  right: [1, 0, 0],
};

const decimalMarks = new Map(); // locale -> ',' or '.'

/** The decimal separator of the current language. */
export function decimalMark() {
  const code = locale();
  if (!decimalMarks.has(code)) {
    let mark = '.';
    try { mark = new Intl.NumberFormat(code).formatToParts(1.5).find((part) => part.type === 'decimal')?.value ?? '.'; } catch { /* unknown locale: a point */ }
    decimalMarks.set(code, mark);
  }
  return decimalMarks.get(code);
}

export function formatMm(value) {
  return Number(value.toFixed(2)).toString().replace('.', decimalMark());
}

// Every label names its unit.
function mm(value) {
  return `${formatMm(value)} mm`;
}

function label(className, text = '') {
  const element = document.createElement('div');
  element.className = `dim-label ${className}`.trim();
  element.textContent = text;
  return new CSS2DObject(element);
}

// A dimension: the line itself, two extension lines, two end ticks, a label.
class Dimension {
  constructor(group, material, className) {
    this.positions = new Float32Array(10 * 3);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(this.positions, 3));
    this.lines = new THREE.LineSegments(geometry, material);
    this.lines.frustumCulled = false;
    this.label = label(className);
    group.add(this.lines, this.label);
  }

  /** `a`,`b`: ends of the dimension line; `fromA`,`fromB`: the points being measured. */
  set(a, b, fromA, fromB, tick, overshoot, text) {
    const along = b.clone().sub(a).normalize();
    const out = a.clone().sub(fromA);
    const outward = out.lengthSq() > 1e-12 ? out.normalize() : new THREE.Vector3();
    const slash = along.clone().add(outward.lengthSq() ? outward : new THREE.Vector3(0, 0, 1)).normalize().multiplyScalar(tick);
    const points = [
      a, b,
      fromA, a.clone().addScaledVector(outward, overshoot),
      fromB, b.clone().addScaledVector(outward, overshoot),
      a.clone().sub(slash), a.clone().add(slash),
      b.clone().sub(slash), b.clone().add(slash),
    ];
    points.forEach((p, i) => p.toArray(this.positions, i * 3));
    this.lines.geometry.attributes.position.needsUpdate = true;
    this.label.position.copy(a).add(b).multiplyScalar(0.5);
    this.label.element.textContent = text;
    this.visible = true;
  }

  set visible(value) {
    this.lines.visible = value;
    this.label.visible = value;
  }

  dispose() {
    this.lines.geometry.dispose();
    this.lines.removeFromParent();
    this.label.removeFromParent();
  }
}

export function createViewer(container) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  container.append(renderer.domElement);
  const labelRenderer = new CSS2DRenderer();
  labelRenderer.domElement.className = 'labels';
  container.append(labelRenderer.domElement);

  // A triad in the corner: which way X, Y and Z point on screen.
  const triad = document.createElementNS(SVG_NS, 'svg');
  triad.setAttribute('class', 'triad');
  triad.setAttribute('viewBox', '-24 -24 48 48');
  triad.setAttribute('aria-hidden', 'true');
  const triadAxes = ['X', 'Y', 'Z'].map((name, index) => {
    const group = document.createElementNS(SVG_NS, 'g');
    const line = document.createElementNS(SVG_NS, 'line');
    const text = document.createElementNS(SVG_NS, 'text');
    text.textContent = name;
    group.append(line, text);
    return { group, line, text, axis: new THREE.Vector3().setComponent(index, 1) };
  });
  container.append(triad);

  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, -1000, 1000);
  camera.position.set(...VIEWS.iso);
  scene.add(camera);
  const ambient = new THREE.AmbientLight(0xffffff, 1.5);
  scene.add(ambient);
  // A headlight: fixed to the camera, so shading stays readable from any side.
  const headlight = new THREE.DirectionalLight(0xffffff, 1.7);
  headlight.position.set(-0.5, 0.9, 1);
  headlight.target.position.set(0, 0, -1);
  camera.add(headlight, headlight.target);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.14;
  controls.zoomToCursor = true;
  controls.zoomSpeed = 1.4;
  controls.rotateSpeed = 0.9;

  const materials = {
    face: new THREE.MeshLambertMaterial({ color: 0xececef, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 }),
    edge: new THREE.LineBasicMaterial({ color: 0x18181b }),
    ghostFace: new THREE.MeshBasicMaterial({ color: 0x71717a, transparent: true, opacity: 0.07, depthWrite: false, side: THREE.DoubleSide }),
    ghostEdge: new THREE.LineBasicMaterial({ color: 0xa1a1aa, transparent: true, opacity: 0.75 }),
    // A scan over a model: a tinted film where they agree, a body where the scan sticks out.
    scan: new THREE.MeshLambertMaterial({ color: 0x3b82f6, transparent: true, opacity: 0.3, depthWrite: false, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -4 }),
    // A scan with no model yet: the thing itself.
    scanAlone: new THREE.MeshLambertMaterial({ color: 0xdde1e7, side: THREE.DoubleSide }),
    dim: new THREE.LineBasicMaterial({ color: 0x3f3f46 }),
    custom: new THREE.LineBasicMaterial({ color: 0x09090b, depthTest: false }),
    span: new THREE.LineBasicMaterial({ color: 0x52525b, depthTest: false }),
    axis: new THREE.LineBasicMaterial({ color: 0xa1a1aa }),
    grid: new THREE.ShaderMaterial({
      transparent: true,
      depthWrite: false,
      side: THREE.DoubleSide,
      uniforms: {
        uCenter: { value: new THREE.Vector2() },
        uRadius: { value: 100 },
        uPixel: { value: 1 },
        uColor: { value: new THREE.Color(0x71717a) },
      },
      vertexShader: GRID_VERTEX,
      fragmentShader: GRID_FRAGMENT,
    }),
  };
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), materials.grid);
  ground.frustumCulled = false;
  const originAxes = new THREE.LineSegments(new THREE.BufferGeometry(), materials.axis);
  originAxes.geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(6 * 3), 3));
  originAxes.frustumCulled = false;

  const groups = {
    grid: new THREE.Group(),
    solid: new THREE.Group(),
    ghost: new THREE.Group(),
    scan: new THREE.Group(),
    dims: new THREE.Group(),
    holes: new THREE.Group(),
    custom: new THREE.Group(),
  };
  scene.add(...Object.values(groups));
  groups.custom.renderOrder = 10;
  groups.grid.add(ground, originAxes);
  ground.visible = originAxes.visible = false;

  const state = {
    meta: null,
    modelBox: null,            // THREE.Box3 of the printed parts
    scanBox: null,             // of the scans being shown
    box: null,                 // what the dimensions measure: the model, else the scans
    frame: null,               // what the camera frames: both
    scanLoadId: 0,
    options: { dims: true, refs: false, grid: true },
    insets: { top: 0, right: 0, bottom: 0, left: 0 },
    loadId: 0,
    overall: [],               // three Dimension objects: X, Y, Z
    customDims: [],            // { dimension, a, b, text }: the model's own dim() calls
    spans: [],                 // the same, for distances between parallel flat faces
    holeLabels: [],            // { label, a, b }
    width: 1,
    height: 1,
    pending: false,
    framed: true,              // the view is as fit() left it: the person has not moved the camera
    move: null,                // a glide of the camera in progress
    dragging: false,
    settleUntil: 0,
    coasting: false,
  };
  const geometryCache = new Map(); // url -> { geometry, edges }
  const scanCache = new Map();     // url -> geometry

  // --- rendering ---------------------------------------------------------

  function worldPerPixel() {
    return FRUSTUM / camera.zoom / state.height;
  }

  function placeTriad() {
    const seen = triadAxes.map((item) => ({ item, v: item.axis.clone().transformDirection(camera.matrixWorldInverse) }));
    seen.sort((a, b) => a.v.z - b.v.z); // the axis nearest the viewer is drawn last
    for (const { item, v } of seen) {
      item.line.setAttribute('x2', (v.x * 11).toFixed(1));
      item.line.setAttribute('y2', (-v.y * 11).toFixed(1));
      item.text.setAttribute('x', (v.x * 18).toFixed(1));
      item.text.setAttribute('y', (-v.y * 18).toFixed(1));
      item.group.style.opacity = v.z < -0.25 ? '0.4' : '1'; // pointing away
      item.text.style.display = Math.hypot(v.x, v.y) < 0.3 ? 'none' : ''; // pointing at the viewer
      triad.append(item.group);
    }
  }

  function render() {
    if (state.pending) return;
    state.pending = true;
    requestAnimationFrame(() => {
      state.pending = false;
      materials.grid.uniforms.uPixel.value = worldPerPixel();
      marker.scale.setScalar(60 * worldPerPixel()); // the same size on screen at any zoom
      placeDimensions();
      placeTriad();
      renderer.render(scene, camera);
      labelRenderer.render(scene, camera);
    });
  }

  function applyViewOffset() {
    // Shifts the picture so the model is centred in the area the panels leave free.
    const { top, right, bottom, left } = state.insets;
    const dx = (right - left) / 2;
    const dy = (bottom - top) / 2;
    if (dx || dy) camera.setViewOffset(state.width, state.height, dx, dy, state.width, state.height);
    else camera.clearViewOffset();
  }

  function resize() {
    const width = Math.max(1, container.clientWidth);
    const height = Math.max(1, container.clientHeight);
    state.width = width;
    state.height = height;
    renderer.setSize(width, height);
    labelRenderer.setSize(width, height);
    const aspect = width / height;
    camera.left = (-FRUSTUM * aspect) / 2;
    camera.right = (FRUSTUM * aspect) / 2;
    camera.top = FRUSTUM / 2;
    camera.bottom = -FRUSTUM / 2;
    applyViewOffset();
    camera.updateProjectionMatrix();
    render();
  }

  // Where the camera should be to frame what is shown, looking along
  // `direction` (from the target toward the camera).
  function framing(direction) {
    const box = state.frame;
    const center = box.getCenter(new THREE.Vector3());
    const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 1);
    // The camera's axes for that direction, without moving the camera.
    const probe = new THREE.Matrix4().lookAt(direction, new THREE.Vector3(), camera.up);
    const right = new THREE.Vector3().setFromMatrixColumn(probe, 0);
    const up = new THREE.Vector3().setFromMatrixColumn(probe, 1);
    let halfW = 0;
    let halfH = 0;
    for (const x of [box.min.x, box.max.x]) {
      for (const y of [box.min.y, box.max.y]) {
        for (const z of [box.min.z, box.max.z]) {
          const corner = new THREE.Vector3(x, y, z).sub(center);
          halfW = Math.max(halfW, Math.abs(corner.dot(right)));
          halfH = Math.max(halfH, Math.abs(corner.dot(up)));
        }
      }
    }
    // Zoom so the box fills part of the free area, with air and room for dimensions around it.
    const { top, right: insetRight, bottom, left } = state.insets;
    const margin = 2 * (GAP_PX + 44);
    const freeW = Math.max(120, state.width - left - insetRight - margin) * FILL;
    const freeH = Math.max(120, state.height - top - bottom - margin) * FILL;
    const perPixel = Math.max((2 * halfW) / freeW, (2 * halfH) / freeH, 1e-6);
    return { center, radius, zoom: FRUSTUM / (perPixel * state.height) };
  }

  function place(center, direction, radius, zoom) {
    controls.target.copy(center);
    camera.position.copy(center).addScaledVector(direction, radius * 6);
    camera.near = -radius * 40;
    camera.far = radius * 40;
    camera.zoom = zoom;
    camera.lookAt(center);
    camera.updateProjectionMatrix();
    camera.updateMatrixWorld();
  }

  /**
   * Frame what is shown, looking along the current direction or a named view.
   * The camera glides there unless `jump` is set.
   */
  function fit(viewName, { jump = false } = {}) {
    if (!state.frame) return;
    const from = {
      center: controls.target.clone(),
      direction: camera.position.clone().sub(controls.target).normalize(),
      zoom: camera.zoom,
    };
    const direction = viewName ? new THREE.Vector3(...VIEWS[viewName]).normalize() : from.direction.clone();
    const to = framing(direction);
    state.framed = true;
    state.move = null;
    const still = from.center.distanceTo(to.center) < 1e-6 && from.direction.angleTo(direction) < 1e-4 && Math.abs(from.zoom / to.zoom - 1) < 1e-4;
    if (jump || still || matchMedia('(prefers-reduced-motion: reduce)').matches) {
      place(to.center, direction, to.radius, to.zoom);
      controls.update();
      render();
      return;
    }
    const turn = new THREE.Quaternion().setFromUnitVectors(from.direction, direction);
    const move = { started: performance.now() };
    state.move = move;
    const step = (now) => {
      if (state.move !== move) return; // superseded, or the person took over
      const t = Math.min(1, (now - move.started) / MOVE_MS);
      const k = 1 - (1 - t) ** 3;
      const facing = from.direction.clone().applyQuaternion(new THREE.Quaternion().slerp(turn, k));
      place(
        from.center.clone().lerp(to.center, k),
        t === 1 ? direction : facing,
        to.radius,
        from.zoom * (to.zoom / from.zoom) ** k,
      );
      controls.update();
      render();
      if (t < 1) requestAnimationFrame(step);
      else state.move = null;
    };
    requestAnimationFrame(step);
  }

  // --- geometry ----------------------------------------------------------

  async function loadGeometry(url) {
    const cached = geometryCache.get(url);
    if (cached) {
      geometryCache.delete(url);
      geometryCache.set(url, cached); // most recently used last
      return cached;
    }
    const response = await fetch(url);
    if (!response.ok) throw new Error(t('Could not load the mesh ({status}).', { status: response.status }));
    let geometry = new STLLoader().parse(await response.arrayBuffer());
    const triangles = geometry.attributes.position.count / 3;
    let edges = null;
    if (triangles <= HEAVY_TRIANGLES) {
      geometry = toCreasedNormals(geometry, CREASE);
      edges = new THREE.EdgesGeometry(geometry, EDGE_ANGLE);
    }
    const entry = { geometry, edges };
    geometryCache.set(url, entry);
    return entry;
  }

  function trimCache(inUse) {
    for (const [url, entry] of geometryCache) {
      if (geometryCache.size <= GEOMETRY_CACHE) break;
      if (inUse.has(url)) continue;
      entry.geometry.dispose();
      entry.edges?.dispose();
      geometryCache.delete(url);
    }
  }

  function rebuildGrid() {
    const box = state.frame;
    ground.visible = originAxes.visible = !!box;
    if (!box) return;
    const size = box.getSize(new THREE.Vector3());
    const span = Math.max(size.x, size.y, size.z * 0.6, 1);
    const center = box.getCenter(new THREE.Vector3());
    const radius = Math.max(span * 2.2, 25);
    ground.position.set(center.x, center.y, box.min.z - 0.01);
    ground.scale.set(radius * 2, radius * 2, 1);
    materials.grid.uniforms.uCenter.value.set(center.x, center.y);
    materials.grid.uniforms.uRadius.value = radius;

    // The origin of the model's coordinate frame; the corner triad names the directions.
    const length = Math.max(span * 0.14, 0.6);
    const position = originAxes.geometry.attributes.position;
    position.array.set([0, 0, 0, length, 0, 0, 0, 0, 0, 0, length, 0, 0, 0, 0, 0, 0, length]);
    position.needsUpdate = true;
  }

  function rebuildAnnotations() {
    for (const dimension of state.overall) dimension.dispose();
    for (const item of [...state.customDims, ...state.spans]) item.dimension.dispose();
    for (const item of state.holeLabels) item.label.removeFromParent();
    state.overall = [];
    state.customDims = [];
    state.spans = [];
    state.holeLabels = [];
    if (!state.box) return;
    state.overall = [0, 1, 2].map(() => new Dimension(groups.dims, materials.dim, ''));
    const meta = state.meta;
    if (!meta) return;
    for (const dim of meta.dims ?? []) {
      state.customDims.push({
        dimension: new Dimension(groups.custom, materials.custom, 'is-custom'),
        a: new THREE.Vector3(...dim.a),
        b: new THREE.Vector3(...dim.b),
        text: `${dim.label} ${mm(dim.value)}`,
      });
    }
    // The same number on the same plane says nothing new: four equal holes in
    // one face get one label, a wall repeated along the part gets one dimension.
    const seen = new Set((meta.dims ?? []).map((dim) => `s|${formatMm(dim.value)}`)); // a length the model already dimensions itself
    const once = (key) => {
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    };
    const direction = (a, b) => {
      const d = new THREE.Vector3(...b).sub(new THREE.Vector3(...a)).normalize();
      if (d.x < -1e-6 || (Math.abs(d.x) < 1e-6 && (d.y < -1e-6 || (Math.abs(d.y) < 1e-6 && d.z < 0)))) d.negate();
      return d;
    };
    const key = (v) => v.toArray().map((c) => c.toFixed(2)).join(',');
    for (const part of meta.parts) {
      for (const span of part.spans ?? []) {
        // Each length is shown once in the whole model: a mirrored or repeated
        // feature, or the same thickness in another direction, adds no number.
        if (!once(`s|${formatMm(span.value)}`)) continue;
        state.spans.push({
          dimension: new Dimension(groups.custom, materials.span, 'is-span'),
          a: new THREE.Vector3(...span.a),
          b: new THREE.Vector3(...span.b),
          text: mm(span.value),
        });
      }
      for (const hole of part.holes ?? []) {
        const axis = direction(hole.a, hole.b);
        if (!once(`h|${formatMm(hole.d)}|${key(axis)}`)) continue; // equal parallel holes: one label
        const tag = label('is-hole', `⌀ ${mm(hole.d)}`);
        groups.holes.add(tag);
        state.holeLabels.push({ label: tag, a: new THREE.Vector3(...hole.a), b: new THREE.Vector3(...hole.b) });
      }
    }
  }

  // Where each annotation goes for the current camera. Runs before every frame.
  function placeDimensions() {
    const box = state.box;
    if (!box) return;
    const dir = camera.getWorldDirection(new THREE.Vector3());
    const perPixel = worldPerPixel();
    const gap = GAP_PX * perPixel;
    const tick = TICK_PX * perPixel;
    const overshoot = OVERSHOOT_PX * perPixel;
    const { min, max } = box;
    const showOverall = state.options.dims;

    // Which way the camera looks along each axis; a tie falls back to the
    // drafting convention (width below, depth to the right, seen from above).
    const sign = (value, tie) => (Math.abs(value) < 0.05 ? tie : Math.sign(value));
    const sx = sign(dir.x, -1);
    const sy = sign(dir.y, 1);
    const sz = sign(dir.z, -1);
    const nearX = sx > 0 ? min.x : max.x;
    const farX = sx > 0 ? max.x : min.x;
    const nearY = sy > 0 ? min.y : max.y;
    const farY = sy > 0 ? max.y : min.y;
    const baseZ = sz < 0 ? min.z : max.z;
    const size = box.getSize(new THREE.Vector3());
    const v = (x, y, z) => new THREE.Vector3(x, y, z);
    const flat = (value) => Math.abs(value) > 0.94; // this axis points into the screen

    const [dimX, dimY, dimZ] = state.overall;
    // Width: along X, pushed out toward the viewer in Y; in a front view, below.
    if (!showOverall || flat(dir.x) || size.x < 1e-6) dimX.visible = false;
    else if (!flat(dir.y)) {
      const y = nearY - sy * gap;
      dimX.set(v(min.x, y, baseZ), v(max.x, y, baseZ), v(min.x, nearY, baseZ), v(max.x, nearY, baseZ), tick, overshoot, mm(size.x));
    } else {
      const z = baseZ + sz * gap;
      dimX.set(v(min.x, nearY, z), v(max.x, nearY, z), v(min.x, nearY, baseZ), v(max.x, nearY, baseZ), tick, overshoot, mm(size.x));
    }
    // Depth: along Y, pushed out in X; in a side view, below.
    if (!showOverall || flat(dir.y) || size.y < 1e-6) dimY.visible = false;
    else if (!flat(dir.x)) {
      const x = nearX - sx * gap;
      dimY.set(v(x, min.y, baseZ), v(x, max.y, baseZ), v(nearX, min.y, baseZ), v(nearX, max.y, baseZ), tick, overshoot, mm(size.y));
    } else {
      const z = baseZ + sz * gap;
      dimY.set(v(nearX, min.y, z), v(nearX, max.y, z), v(nearX, min.y, baseZ), v(nearX, max.y, baseZ), tick, overshoot, mm(size.y));
    }
    // Height: on the silhouette edge away from the other two, pushed out sideways.
    if (!showOverall || flat(dir.z) || size.z < 1e-6) dimZ.visible = false;
    else if (!flat(dir.x)) {
      const x = farX + sx * gap;
      dimZ.set(v(x, nearY, min.z), v(x, nearY, max.z), v(farX, nearY, min.z), v(farX, nearY, max.z), tick, overshoot, mm(size.z));
    } else {
      const y = farY + sy * gap;
      dimZ.set(v(nearX, y, min.z), v(nearX, y, max.z), v(nearX, farY, min.z), v(nearX, farY, max.z), tick, overshoot, mm(size.z));
    }

    for (const item of [...state.customDims, ...state.spans]) {
      item.dimension.set(item.a, item.b, item.a, item.b, tick, 0, item.text);
      // Seen end-on, a dimension is a dot with a label: leave it out.
      const along = item.b.clone().sub(item.a);
      const onScreen = along.clone().addScaledVector(dir, -along.dot(dir)).length() / perPixel;
      item.dimension.visible = state.options.dims && onScreen > 14;
    }
    // A hole is labelled at whichever end of it faces the viewer. A label that
    // would land on a dimension's label or on another hole's (a counterbore is
    // two holes on one axis) steps down a row; with no free row it hides.
    camera.updateMatrixWorld();
    const taken = [];
    const rectOf = (position, text) => {
      const p = position.clone().project(camera);
      const w = 12 + text.length * 6.7;
      return { x: ((p.x + 1) / 2) * state.width - w / 2, y: ((1 - p.y) / 2) * state.height - ROW_PX / 2, w, h: ROW_PX };
    };
    const hits = (r) => taken.some((t) => r.x < t.x + t.w && t.x < r.x + r.w && r.y < t.y + t.h && t.y < r.y + r.h);
    for (const item of state.customDims) if (item.dimension.label.visible) taken.push(rectOf(item.dimension.label.position, item.text));
    for (const dimension of state.overall) {
      if (!dimension.label.visible) continue;
      const rect = rectOf(dimension.label.position, dimension.label.element.textContent);
      // A dim() from the model that measures the same thing wins.
      if (hits(rect)) dimension.visible = false;
      else taken.push(rect);
    }
    // Distances between flat faces fill in where there is room left.
    for (const item of state.spans) {
      if (!item.dimension.label.visible) continue;
      const rect = rectOf(item.dimension.label.position, item.text);
      if (hits(rect)) item.dimension.visible = false;
      else taken.push(rect);
    }
    for (const item of state.holeLabels) {
      item.label.position.copy(item.a.dot(dir) < item.b.dot(dir) ? item.a : item.b);
      let visible = state.options.dims;
      if (visible) {
        const rect = rectOf(item.label.position, item.label.element.textContent);
        let row = 0;
        while (row < 3 && hits(rect)) {
          row += 1;
          rect.y += ROW_PX;
        }
        visible = !hits(rect);
        if (visible) {
          taken.push(rect);
          item.label.center.set(0.5, 0.5 - (row * ROW_PX) / 19);
        }
      }
      item.label.visible = visible;
    }
  }

  // --- public ------------------------------------------------------------

  /**
   * Show a build. `urlFor(file)` gives the address of one of its STL files.
   * The camera stays where it is unless there was no model before, the model
   * changed size drastically, or `fitView` is true.
   */
  // One picture into the next. What is on the stage now is kept as a picture over it and faded away,
  // while what is put up next fades in under it: another model, another version, an STL. The two are
  // added together (style.css, .was), so what they share (the grid, an unchanged part) stays still.
  let fading = null;
  function fadeOver() {
    const canvas = renderer.domElement;
    if (matchMedia('(prefers-reduced-motion: reduce)').matches || !canvas.width || !canvas.height) return;
    if (fading) {
      if (performance.now() - fading.since < 120) return; // one that has just begun covers this change too
      for (const node of fading.nodes) node.remove();
    }
    placeDimensions();
    renderer.render(scene, camera); // read right after drawing: the buffer is not kept
    const picture = document.createElement('canvas');
    picture.width = canvas.width;
    picture.height = canvas.height;
    picture.getContext('2d').drawImage(canvas, 0, 0);
    const labels = labelRenderer.domElement.cloneNode(true);
    for (const node of [picture, labels]) node.classList.add('was');
    canvas.after(picture);
    labelRenderer.domElement.after(labels);
    const mine = { nodes: [picture, labels], since: performance.now() };
    fading = mine;
    const timing = { duration: 340, easing: 'ease-in-out' };
    const full = getComputedStyle(canvas).opacity;
    for (const node of [canvas, labelRenderer.domElement]) node.animate([{ opacity: 0 }, { opacity: full }], timing);
    Promise.allSettled(mine.nodes.map((node) => node.animate([{ opacity: full }, { opacity: 0 }], { ...timing, fill: 'forwards' }).finished)).then(() => {
      for (const node of mine.nodes) node.remove();
      if (fading === mine) fading = null;
    });
  }

  /** Put a built model on the stage (`meta` of a build), or take it off (null). With `dissolve`, the picture before fades into it. */
  async function show(meta, urlFor, { fitView = false, dissolve = false } = {}) {
    const loadId = ++state.loadId;
    if (!meta?.ok) {
      if (dissolve) fadeOver();
      state.meta = null;
      state.modelBox = null;
      for (const group of [groups.solid, groups.ghost]) group.clear();
      reframe();
      return;
    }
    const loaded = await Promise.all(meta.parts.map(async (part) => {
      const url = urlFor(part.file);
      return { part, url, ...(await loadGeometry(url)) };
    }));
    if (loadId !== state.loadId) return; // a newer build arrived meanwhile

    if (dissolve) fadeOver();
    for (const group of [groups.solid, groups.ghost]) group.clear();
    for (const { part, geometry, edges } of loaded) {
      const printed = part.kind === 'show';
      const mesh = new THREE.Mesh(geometry, printed ? materials.face : materials.ghostFace);
      mesh.name = part.name;
      (printed ? groups.solid : groups.ghost).add(mesh);
      if (edges) (printed ? groups.solid : groups.ghost).add(new THREE.LineSegments(edges, printed ? materials.edge : materials.ghostEdge));
    }
    trimCache(new Set(loaded.map((item) => item.url)));

    state.meta = meta;
    state.modelBox = new THREE.Box3(new THREE.Vector3(...meta.bbox[0]), new THREE.Vector3(...meta.bbox[1]));
    reframe(fitView);
  }

  // After the model or the scans changed: what is measured, what is framed.
  function reframe(fitView = false) {
    const previous = state.frame;
    state.box = state.modelBox ?? state.scanBox;
    state.frame = state.modelBox && state.scanBox ? state.modelBox.clone().union(state.scanBox) : state.box;
    for (const mesh of groups.scan.children) mesh.material = state.modelBox ? materials.scan : materials.scanAlone;
    rebuildGrid();
    rebuildAnnotations();
    if (!state.frame) {
      render();
      return;
    }
    // Until the person moves the camera themselves, every change is framed anew.
    let refit = fitView || !previous || (state.framed && !previous.equals(state.frame));
    if (!refit) {
      const before = previous.getSize(new THREE.Vector3()).length();
      const after = state.frame.getSize(new THREE.Vector3()).length();
      const moved = previous.getCenter(new THREE.Vector3()).distanceTo(state.frame.getCenter(new THREE.Vector3()));
      refit = after > before * 1.8 || after < before * 0.55 || moved > Math.max(before, after);
    }
    if (refit) fit(previous ? undefined : 'iso', { jump: !previous || fitView });
    else render();
  }

  async function loadScan(url) {
    const cached = scanCache.get(url);
    if (cached) return cached;
    const response = await fetch(url);
    if (!response.ok) throw new Error(t('Could not load the STL ({status}).', { status: response.status }));
    const geometry = new STLLoader().parse(await response.arrayBuffer());
    geometry.computeBoundingBox();
    scanCache.set(url, geometry);
    return geometry;
  }

  const raycaster = new THREE.Raycaster();
  const marker = new THREE.Group();
  {
    // The spot as a draughtsman would shade it: a field of dots, large at the
    // spot and thinning out to nothing, with no edge anywhere.
    const size = 512;
    const pitch = 18;
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = size;
    const g = canvas.getContext('2d');
    g.fillStyle = '#fff';
    const middle = size / 2;
    for (let row = 0; row * pitch <= size; row += 1) {
      for (let column = 0; column * pitch <= size; column += 1) {
        // Every other row is shifted by half a step, as in a printed halftone.
        const x = column * pitch + (row % 2 ? pitch / 2 : 0);
        const y = row * pitch * 0.866;
        const far = Math.hypot(x - middle, y - middle) / middle;
        if (far >= 1) continue;
        g.beginPath();
        g.arc(x, y, pitch * 0.36 * (1 - far) ** 0.9, 0, Math.PI * 2);
        g.fill();
      }
    }
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    materials.markerGlow = new THREE.MeshBasicMaterial({ map: texture, color: 0x2643a6, transparent: true, depthTest: false, depthWrite: false, side: THREE.DoubleSide });
    const halo = new THREE.Mesh(new THREE.CircleGeometry(1, 48), materials.markerGlow);
    halo.renderOrder = 20;
    marker.add(halo);
    marker.visible = false;
    scene.add(marker);
  }

  /**
   * What is under a point of the screen (client coordinates): the part or scan,
   * the spot on its surface and which way that surface faces. Null for empty space.
   */
  function pick(clientX, clientY) {
    const rect = renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
    camera.updateMatrixWorld();
    raycaster.setFromCamera(ndc, camera);
    const targets = [...groups.solid.children, ...(groups.ghost.visible ? groups.ghost.children : []), ...groups.scan.children].filter((object) => object.isMesh);
    const hit = raycaster.intersectObjects(targets, false)[0];
    if (!hit) return null;
    const normal = hit.face.normal.clone().transformDirection(hit.object.matrixWorld);
    if (normal.dot(raycaster.ray.direction) > 0) normal.negate(); // the side facing the viewer
    const kind = groups.scan.children.includes(hit.object) ? 'scan' : groups.ghost.children.includes(hit.object) ? 'ref' : 'part';
    return { name: hit.object.name, kind, point: hit.point.toArray(), normal: normal.toArray() };
  }

  /** Mark a spot on the model (as `pick` returned it), or clear the mark. */
  function setMarker(spot) {
    marker.visible = !!spot;
    if (spot) {
      marker.position.fromArray(spot.point);
      marker.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), new THREE.Vector3().fromArray(spot.normal));
    }
    render();
  }

  /**
   * What the person sees around a 3D point: a square cut from the view, centred
   * on it, as base64 JPEG (for Claude, next to a pick). Null when off screen.
   */
  function snapshot(point, half = 190, out = 480) {
    const canvas = renderer.domElement;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    const at = new THREE.Vector3().fromArray(point).project(camera);
    if (Math.abs(at.x) > 1 || Math.abs(at.y) > 1) return null;
    const side = Math.min(2 * half, width, height);
    const cx = Math.min(Math.max((at.x + 1) / 2 * width, side / 2), width - side / 2);
    const cy = Math.min(Math.max((1 - at.y) / 2 * height, side / 2), height - side / 2);
    const scale = canvas.width / width;
    placeDimensions();
    renderer.render(scene, camera); // read right after drawing: the buffer is not kept
    const cut = document.createElement('canvas');
    cut.width = cut.height = Math.min(out, Math.round(side * scale));
    const g = cut.getContext('2d');
    g.fillStyle = getComputedStyle(canvas.parentElement).backgroundColor || '#fff';
    if (g.fillStyle === 'rgba(0, 0, 0, 0)') g.fillStyle = '#fff';
    g.fillRect(0, 0, cut.width, cut.height);
    g.drawImage(canvas, (cx - side / 2) * scale, (cy - side / 2) * scale, side * scale, side * scale, 0, 0, cut.width, cut.height);
    return cut.toDataURL('image/jpeg', 0.85).split(',')[1];
  }

  // Colours of the scene by day and by night; the page around it follows CSS.
  const PALETTES = {
    light: { face: 0xf1eee7, edge: 0x1c1b18, ghostFace: 0x6f6a5e, ghostEdge: 0xa39d8f, scanAlone: 0xdfe2ea, dim: 0x4a463d, custom: 0x1c1b18, span: 0x5a5548, axis: 0xa39d8f, markerGlow: 0x2643a6, grid: 0x6f6a5e, ambient: 1.5, head: 1.7 },
    dark: { face: 0x666872, edge: 0xeceae4, ghostFace: 0x9a968b, ghostEdge: 0x6b685f, scanAlone: 0x5b6070, dim: 0x9a968b, custom: 0xeceae4, span: 0x9a968b, axis: 0x55534c, markerGlow: 0x8fa4f5, grid: 0x8b8a84, ambient: 1.2, head: 1.9 },
  };
  function setTheme(name) {
    const palette = PALETTES[name] ?? PALETTES.light;
    for (const key of ['face', 'edge', 'ghostFace', 'ghostEdge', 'scanAlone', 'dim', 'custom', 'span', 'axis', 'markerGlow']) materials[key].color.setHex(palette[key]);
    materials.grid.uniforms.uColor.value.setHex(palette.grid);
    ambient.intensity = palette.ambient;
    headlight.intensity = palette.head;
    render();
  }

  /** Draw these scans ([{ name, url }]) with the model; an empty list hides them. */
  async function setScans(list, { dissolve = false } = {}) {
    const loadId = ++state.scanLoadId;
    const loaded = await Promise.all(list.map(async (scan) => ({ scan, geometry: await loadScan(scan.url) })));
    if (loadId !== state.scanLoadId) return; // a newer list arrived meanwhile
    if (dissolve) fadeOver();
    groups.scan.clear();
    let box = null;
    for (const { scan, geometry } of loaded) {
      const mesh = new THREE.Mesh(geometry, materials.scan);
      mesh.name = scan.name;
      groups.scan.add(mesh);
      box = box ? box.union(geometry.boundingBox) : geometry.boundingBox.clone();
    }
    const inUse = new Set(list.map((scan) => scan.url));
    for (const [url, geometry] of scanCache) {
      if (scanCache.size <= SCAN_CACHE) break;
      if (inUse.has(url)) continue;
      geometry.dispose();
      scanCache.delete(url);
    }
    state.scanBox = box;
    reframe();
  }

  function setOptions(options) {
    Object.assign(state.options, options);
    groups.ghost.visible = state.options.refs;
    groups.grid.visible = state.options.grid;
    render();
  }

  // The numbers on the labels follow the language: write them again after it changed.
  function relabel() {
    rebuildAnnotations();
    render();
  }

  function setInsets(insets) {
    const next = { ...state.insets, ...insets };
    if (['top', 'right', 'bottom', 'left'].every((side) => next[side] === state.insets[side])) return;
    state.insets = next;
    container.style.setProperty('--inset-right', `${next.right}px`);
    applyViewOffset();
    camera.updateProjectionMatrix();
    // A panel opened or closed: frame the model again in the room that is left.
    if (state.frame) fit();
    else render();
  }

  // While the person drags, and for a moment after, the camera is updated every
  // frame so it can coast to a stop; otherwise nothing runs.
  function coast() {
    if (!state.dragging && performance.now() > state.settleUntil) {
      state.coasting = false;
      return;
    }
    controls.update();
    requestAnimationFrame(coast);
  }
  controls.addEventListener('change', render);
  controls.addEventListener('start', () => {
    state.framed = false;
    state.move = null;
    state.dragging = true;
    if (!state.coasting) {
      state.coasting = true;
      requestAnimationFrame(coast);
    }
  });
  controls.addEventListener('end', () => {
    state.dragging = false;
    state.settleUntil = performance.now() + SETTLE_MS;
  });
  const observer = new ResizeObserver(resize);
  observer.observe(container);
  resize();

  return {
    show,
    setScans,
    pick,
    setMarker,
    snapshot,
    setTheme,
    fit,
    setOptions,
    relabel,
    setInsets,
    view: (name) => fit(name),
    dispose() {
      observer.disconnect();
      controls.dispose();
      // Loads still in flight are dropped when they land.
      state.loadId += 1;
      state.scanLoadId += 1;
      for (const dimension of state.overall) dimension.dispose();
      for (const item of [...state.customDims, ...state.spans]) item.dimension.dispose();
      for (const entry of geometryCache.values()) {
        entry.geometry.dispose();
        entry.edges?.dispose();
      }
      geometryCache.clear();
      for (const geometry of scanCache.values()) geometry.dispose();
      scanCache.clear();
      ground.geometry.dispose();
      originAxes.geometry.dispose();
      marker.children[0].geometry.dispose();
      materials.markerGlow.map.dispose();
      for (const material of Object.values(materials)) material.dispose();
      renderer.dispose();
      renderer.domElement.remove();
      labelRenderer.domElement.remove();
      triad.remove();
    },
  };
}
