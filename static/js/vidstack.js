class MediaPlayer extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._video = document.createElement('video');
    this._video.style.width = '100%';
    this._video.style.height = '100%';
    this._video.controls = true;
    this.shadowRoot.appendChild(this._video);
  }

  static get observedAttributes() {
    return ['src', 'autoplay', 'controls', 'loop', 'muted', 'playsinline'];
  }

  attributeChangedCallback(name, oldValue, newValue) {
    if (oldValue !== newValue) {
      this._video[name] = newValue;
    }
  }

  connectedCallback() {
    const src = this.getAttribute('src');
    if (src) this._video.src = src;
    
    // Copy other attributes
    ['autoplay', 'controls', 'loop', 'muted', 'playsinline'].forEach(attr => {
      if (this.hasAttribute(attr)) {
        this._video[attr] = this.getAttribute(attr) !== 'false';
      }
    });
  }
}

customElements.define('media-player', MediaPlayer);
