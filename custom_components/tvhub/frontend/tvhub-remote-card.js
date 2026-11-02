/* TVHub remote card for Home Assistant — d-pad + keys, same layout as the TVHub web remote.
   Usage:  type: custom:tvhub-remote
           entity: media_player.<your TV>
   Optional: name, show_power (default true), show_volume (default true), show_media (default true). */
const TEXT = {
  en: { back: "Back", home: "Home", menu: "Menu", rew: "Rewind", play: "Play", ff: "Forward", vd: "Vol −", mute: "Mute", vu: "Vol +",
        up: "Up", down: "Down", left: "Left", right: "Right", ok: "OK", power: "Power", unavailable: "Unavailable", pick: "Choose a TVHub media player" },
  es: { back: "Atrás", home: "Inicio", menu: "Menú", rew: "Retroceder", play: "Play", ff: "Avanzar", vd: "Vol −", mute: "Mute", vu: "Vol +",
        up: "Arriba", down: "Abajo", left: "Izquierda", right: "Derecha", ok: "OK", power: "Encender / apagar", unavailable: "No disponible", pick: "Elige un reproductor TVHub" },
};
const REPEAT_AFTER = 420, REPEAT_EVERY = 130;      // ms: hold an arrow or a volume key to repeat it
const REPEATABLE = new Set(["DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "VOLUME_UP", "VOLUME_DOWN"]);

const STYLE = `
:host { display: block; }
ha-card { padding: 16px; overflow: hidden; }
.head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 14px; }
.title { font-size: var(--ha-card-header-font-size, 20px); font-weight: 500; line-height: 1.2; }
.sub { color: var(--secondary-text-color); font-size: 13px; margin-top: 2px; }
button { font: inherit; color: var(--primary-text-color); cursor: pointer; -webkit-tap-highlight-color: transparent;
  touch-action: manipulation; user-select: none; -webkit-user-select: none; }
button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
ha-icon { --mdc-icon-size: 22px; pointer-events: none; }
.power { width: 40px; height: 40px; border-radius: 50%; border: 1px solid var(--divider-color); background: var(--secondary-background-color);
  display: flex; align-items: center; justify-content: center; padding: 0; }
.pad { position: relative; width: min(100%, 260px); aspect-ratio: 1; margin: 0 auto 16px; border-radius: 50%;
  border: 1px solid var(--divider-color);
  background: radial-gradient(circle at 50% 30%, color-mix(in srgb, var(--primary-text-color) 10%, var(--card-background-color)) 0%,
              color-mix(in srgb, var(--primary-text-color) 2%, var(--card-background-color)) 72%);
  box-shadow: inset 0 -14px 28px rgba(0,0,0,.16); }
.pad button { position: absolute; background: none; border: 0; padding: 0; display: flex; align-items: center; justify-content: center; }
.pad ha-icon { --mdc-icon-size: 28px; }
.pad .u { top: 5%; left: 50%; width: 34%; height: 26%; transform: translateX(-50%); border-radius: 50% 50% 8px 8px; }
.pad .d { bottom: 5%; left: 50%; width: 34%; height: 26%; transform: translateX(-50%); border-radius: 8px 8px 50% 50%; }
.pad .l { left: 5%; top: 50%; width: 26%; height: 34%; transform: translateY(-50%); border-radius: 50% 8px 8px 50%; }
.pad .r { right: 5%; top: 50%; width: 26%; height: 34%; transform: translateY(-50%); border-radius: 8px 50% 50% 8px; }
.pad .ok { top: 50%; left: 50%; width: 34%; aspect-ratio: 1; transform: translate(-50%, -50%); border-radius: 50%;
  border: 1px solid var(--divider-color); background: var(--secondary-background-color); font-weight: 600; letter-spacing: .04em; }
.pad button:active { background: color-mix(in srgb, var(--primary-color) 28%, transparent); }
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.grid button { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 11px 4px; font-size: 12px;
  border-radius: 14px; border: 1px solid var(--divider-color); background: var(--secondary-background-color); }
.grid button:active, .power:active, .pad .ok:active { background: color-mix(in srgb, var(--primary-color) 28%, var(--secondary-background-color)); }
.grid button:active, .power:active { transform: scale(.96); }
.pad .ok:active { transform: translate(-50%, -50%) scale(.96); }
.off .pad, .off .grid { opacity: .55; }
.gone .pad, .gone .grid, .gone .power { opacity: .3; pointer-events: none; }
.hint { color: var(--secondary-text-color); padding: 8px 0; }
`;

class TVHubRemoteCard extends HTMLElement {
  static getStubConfig(hass) {
    const ids = Object.keys(hass?.states || {}).filter((e) => e.startsWith("media_player."));
    const mine = ids.find((e) => hass.entities?.[e]?.platform === "tvhub");
    return { entity: mine || ids[0] || "" };
  }
  static getConfigForm() {
    return { schema: [
      { name: "entity", required: true, selector: { entity: { domain: "media_player", integration: "tvhub" } } },
      { name: "name", selector: { text: {} } },
      { name: "show_power", selector: { boolean: {} } },
      { name: "show_volume", selector: { boolean: {} } },
      { name: "show_media", selector: { boolean: {} } },
    ] };
  }

  setConfig(config) {
    if (!config || typeof config.entity !== "string" || !config.entity.startsWith("media_player.")) {
      throw new Error("TVHub card: set 'entity' to one of your TVHub media_player entities");
    }
    this._config = { show_power: true, show_volume: true, show_media: true, ...config };
    this._build();
    this._update();
  }

  set hass(hass) { this._hass = hass; this._update(); }
  getCardSize() { return 9; }
  getGridOptions() { return { columns: 6, rows: 8, min_columns: 4 }; }
  disconnectedCallback() { this._stopRepeat(); }

  get _t() { return TEXT[(this._hass?.language || "en").slice(0, 2)] || TEXT.en; }

  _build() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const c = this._config, t = this._t;
    const key = (k, icon, label) => `<button type="button" data-key="${k}" aria-label="${label}"><ha-icon icon="${icon}"></ha-icon>${label}</button>`;
    const rows = [
      [["BACK", "mdi:undo-variant", t.back], ["HOME", "mdi:home-outline", t.home], ["MENU", "mdi:menu", t.menu]],
      c.show_media && [["MEDIA_REWIND", "mdi:rewind", t.rew], ["MEDIA_PLAY_PAUSE", "mdi:play", t.play], ["MEDIA_FAST_FORWARD", "mdi:fast-forward", t.ff]],
      c.show_volume && [["VOLUME_DOWN", "mdi:volume-low", t.vd], ["VOLUME_MUTE", "mdi:volume-off", t.mute], ["VOLUME_UP", "mdi:volume-high", t.vu]],
    ].filter(Boolean).flat().map((k) => key(...k)).join("");
    this.shadowRoot.innerHTML = `<style>${STYLE}</style>
      <ha-card>
        <div class="head"><div><div class="title"></div><div class="sub"></div></div>
          ${c.show_power ? `<button type="button" class="power" data-power aria-label="${t.power}" title="${t.power}"><ha-icon icon="mdi:power"></ha-icon></button>` : ""}</div>
        <div class="pad">
          <button type="button" class="u" data-key="DPAD_UP" aria-label="${t.up}"><ha-icon icon="mdi:chevron-up"></ha-icon></button>
          <button type="button" class="l" data-key="DPAD_LEFT" aria-label="${t.left}"><ha-icon icon="mdi:chevron-left"></ha-icon></button>
          <button type="button" class="ok" data-key="DPAD_CENTER" aria-label="${t.ok}">${t.ok}</button>
          <button type="button" class="r" data-key="DPAD_RIGHT" aria-label="${t.right}"><ha-icon icon="mdi:chevron-right"></ha-icon></button>
          <button type="button" class="d" data-key="DPAD_DOWN" aria-label="${t.down}"><ha-icon icon="mdi:chevron-down"></ha-icon></button>
        </div>
        <div class="grid">${rows}</div>
      </ha-card>`;
    const root = this.shadowRoot;
    root.querySelectorAll("[data-key]").forEach((b) => {
      const k = b.dataset.key;
      b.addEventListener("pointerdown", (e) => { if (e.button) return; this._press(k); if (REPEATABLE.has(k)) this._startRepeat(k); });
      ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => b.addEventListener(ev, () => this._stopRepeat()));
      b.addEventListener("click", (e) => { if (e.detail === 0) this._press(k); });        // keyboard (Enter / Space)
      b.addEventListener("contextmenu", (e) => e.preventDefault());
    });
    const pw = root.querySelector("[data-power]");
    if (pw) pw.addEventListener("click", () => {
      const off = this._state()?.state === "off";
      this._hass?.callService("media_player", off ? "turn_on" : "turn_off", {}, { entity_id: this._config.entity });
    });
  }

  _state() { return this._hass?.states?.[this._config?.entity]; }

  _update() {
    if (!this.shadowRoot || !this._config || !this._hass) return;
    const st = this._state(), t = this._t, card = this.shadowRoot.querySelector("ha-card");
    const gone = !st || st.state === "unavailable";
    card.classList.toggle("gone", gone);
    card.classList.toggle("off", !gone && st.state === "off");
    this.shadowRoot.querySelector(".title").textContent = this._config.name || st?.attributes?.friendly_name || this._config.entity;
    const app = st?.state === "on" && st.attributes.app_name ? ` · ${st.attributes.app_name}` : "";
    const label = gone ? t.unavailable : (this._hass.formatEntityState ? this._hass.formatEntityState(st) : st.state);
    this.shadowRoot.querySelector(".sub").textContent = label + app;
  }

  _press(key) {
    this._hass?.callService("tvhub", "send_key", { key }, { entity_id: this._config.entity });
    if (navigator.vibrate) { try { navigator.vibrate(8); } catch (e) { /* not supported */ } }
  }
  _startRepeat(key) {
    this._stopRepeat();
    this._rt = setTimeout(() => { this._ri = setInterval(() => this._press(key), REPEAT_EVERY); }, REPEAT_AFTER);
  }
  _stopRepeat() { clearTimeout(this._rt); clearInterval(this._ri); this._rt = this._ri = null; }
}

if (!customElements.get("tvhub-remote")) customElements.define("tvhub-remote", TVHubRemoteCard);
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "tvhub-remote")) {
  window.customCards.push({ type: "tvhub-remote", name: "TVHub Remote", preview: false,
    description: "D-pad and keys for a TV managed by TVHub (Android TV / Fire TV, LG webOS, Hisense VIDAA)." });
}
