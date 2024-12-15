class MediaProvider extends HTMLElement {
  constructor() {
    super();
    this.video = document.createElement('video');
    this.appendChild(this.video);
  }

  set src(value) {
    this.video.src = value;
  }

  get src() {
    return this.video.src;
  }
}

class MediaPlayer extends HTMLElement {
  constructor() {
    super();
    this.provider = this.querySelector('media-provider');
    this.video = this.provider?.querySelector('video');
  }

  pause() {
    this.video?.pause();
  }

  play() {
    return this.video?.play();
  }

  get duration() {
    return this.video?.duration || 0;
  }

  get currentTime() {
    return this.video?.currentTime || 0;
  }
}

class MediaTimeSlider extends HTMLElement {
  constructor() {
    super();
    this.addEventListener('click', this.handleClick.bind(this));
  }

  handleClick(event) {
    const player = this.closest('media-player');
    const video = player?.querySelector('video');
    if (!video) return;

    const rect = this.getBoundingClientRect();
    const percent = (event.clientX - rect.left) / rect.width;
    video.currentTime = video.duration * percent;
  }
}

class MediaPlayButton extends HTMLElement {
  constructor() {
    super();
    this.innerHTML = '▶';
    this.addEventListener('click', this.handleClick.bind(this));
  }

  handleClick() {
    const player = this.closest('media-player');
    const video = player?.querySelector('video');
    if (!video) return;

    if (video.paused) {
      video.play();
      this.innerHTML = '⏸';
    } else {
      video.pause();
      this.innerHTML = '▶';
    }
  }
}

class MediaMuteButton extends HTMLElement {
  constructor() {
    super();
    this.innerHTML = '🔊';
    this.addEventListener('click', this.handleClick.bind(this));
  }

  handleClick() {
    const player = this.closest('media-player');
    const video = player?.querySelector('video');
    if (!video) return;

    video.muted = !video.muted;
    this.innerHTML = video.muted ? '🔇' : '🔊';
  }
}

class MediaFullscreenButton extends HTMLElement {
  constructor() {
    super();
    this.innerHTML = '⛶';
    this.addEventListener('click', this.handleClick.bind(this));
  }

  handleClick() {
    const player = this.closest('media-player');
    if (!player) return;

    if (!document.fullscreenElement) {
      player.requestFullscreen();
    } else {
      document.exitFullscreen();
    }
  }
}

// Register custom elements
customElements.define('media-provider', MediaProvider);
customElements.define('media-player', MediaPlayer);
customElements.define('media-time-slider', MediaTimeSlider);
customElements.define('media-play-button', MediaPlayButton);
customElements.define('media-mute-button', MediaMuteButton);
customElements.define('media-fullscreen-button', MediaFullscreenButton);
