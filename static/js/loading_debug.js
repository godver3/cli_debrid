// DEBUG: Loading state tracker
window.LoadingDebug = {
    calls: [],
    logCall: function(method, args) {
        const entry = {
            timestamp: Date.now(),
            method: method,
            args: args,
            currentProgress: Loading.currentProgress,
            timerActive: Loading.progressTimer !== null,
            startTime: Loading.startTime
        };
        this.calls.push(entry);
        console.log(`[LOADING DEBUG] ${method}:`, entry);

        // Keep only last 50 calls
        if (this.calls.length > 50) {
            this.calls.shift();
        }
    },
    dumpHistory: function() {
        console.table(this.calls);
    }
};

// Wrap Loading methods to add debug logging
const originalShow = Loading.show;
Loading.show = function(...args) {
    window.LoadingDebug.logCall('show', args);
    return originalShow.apply(this, args);
};

const originalHide = Loading.hide;
Loading.hide = function(...args) {
    window.LoadingDebug.logCall('hide', args);
    return originalHide.apply(this, args);
};

const originalSetProgress = Loading.setProgress;
Loading.setProgress = function(progress) {
    window.LoadingDebug.logCall('setProgress', [progress]);
    return originalSetProgress.call(this, progress);
};

const originalStartProgressTimer = Loading.startProgressTimer;
Loading.startProgressTimer = function() {
    window.LoadingDebug.logCall('startProgressTimer', []);
    return originalStartProgressTimer.call(this);
};

const originalStopProgressTimer = Loading.stopProgressTimer;
Loading.stopProgressTimer = function() {
    window.LoadingDebug.logCall('stopProgressTimer', []);
    return originalStopProgressTimer.call(this);
};

console.log('[LOADING DEBUG] Debug wrapper installed. Use window.LoadingDebug.dumpHistory() to see call history.');
