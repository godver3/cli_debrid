// Zabby was here.
//
// A small thank-you left by Zabby (github.com/zab1996), who worked on cli_debrid and
// cli_mount from August 2026: a note in the browser console for whoever opens dev
// tools, and a toast for whoever knows the Konami code (up up down down left right
// left right b a). Harmless, self-contained, and safe to delete.
(function () {
    'use strict';

    var CREDIT = 'Helped build cli_debrid & cli_mount \u00b7 since August 2026';

    try {
        console.log(
            '%c' + [
                ' _____     _     _             __        __           _   _               ',
                '|__  /__ _| |__ | |__  _   _   \\ \\      / /_ _ ___   | | | | ___ _ __ ___ ',
                '  / // _` | \'_ \\| \'_ \\| | | |   \\ \\ /\\ / / _` / __|  | |_| |/ _ \\ \'__/ _ \\',
                ' / /| (_| | |_) | |_) | |_| |    \\ V  V / (_| \\__ \\  |  _  |  __/ | |  __/',
                '/____\\__,_|_.__/|_.__/ \\__, |     \\_/\\_/ \\__,_|___/  |_| |_|\\___|_|  \\___|',
                '                       |___/                                              ',
            ].join('\n') + '\n\n%c' + CREDIT + '. Thanks for reading the source \ud83d\udc4b',
            'color:#f97316;font-family:monospace;font-weight:bold',
            'color:#a3a3a3;font-style:italic'
        );
    } catch (e) { /* no console */ }

    var KONAMI = ['ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
                  'ArrowLeft', 'ArrowRight', 'b', 'a'];
    var position = 0;

    function typingInAField(target) {
        if (!target) return false;
        var tag = (target.tagName || '').toLowerCase();
        return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable;
    }

    function showToast() {
        if (document.getElementById('zabby-was-here')) return;
        var toast = document.createElement('div');
        toast.id = 'zabby-was-here';
        toast.setAttribute('role', 'status');
        toast.style.cssText = [
            'position:fixed', 'left:50%', 'bottom:32px', 'transform:translate(-50%, 20px)',
            'z-index:100000', 'max-width:calc(100vw - 32px)', 'padding:14px 20px',
            'border-radius:12px', 'background:#1c1917', 'color:#fafaf9',
            'border:1px solid #f97316', 'box-shadow:0 10px 30px rgba(0,0,0,.45)',
            'font:15px/1.4 system-ui,-apple-system,Segoe UI,sans-serif', 'text-align:center',
            'opacity:0', 'transition:opacity .3s ease, transform .3s ease', 'cursor:pointer'
        ].join(';');
        toast.innerHTML = '<div style="font-size:18px;font-weight:700">Zabby was here \ud83d\udc4b</div>' +
            '<div style="opacity:.75;font-size:13px;margin-top:2px">' + CREDIT + '</div>';
        document.body.appendChild(toast);
        requestAnimationFrame(function () {
            toast.style.opacity = '1';
            toast.style.transform = 'translate(-50%, 0)';
        });
        function hide() {
            toast.style.opacity = '0';
            toast.style.transform = 'translate(-50%, 20px)';
            setTimeout(function () { toast.remove(); }, 350);
        }
        toast.addEventListener('click', hide);
        setTimeout(hide, 6000);
    }

    document.addEventListener('keydown', function (event) {
        if (typingInAField(event.target)) { position = 0; return; }
        var key = event.key && event.key.length === 1 ? event.key.toLowerCase() : event.key;
        if (key === KONAMI[position]) {
            position += 1;
            if (position === KONAMI.length) {
                position = 0;
                showToast();
            }
        } else if (key === 'ArrowUp') {
            // An extra up still counts as the start: up, up, up, down ... works.
            position = position >= 2 && KONAMI[position - 1] === 'ArrowUp' ? 2 : 1;
        } else {
            position = 0;
        }
    });
})();
