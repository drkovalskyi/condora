/**
 * Alpine refreshController — manages auto-refresh interval.
 */
document.addEventListener('alpine:init', () => {
    Alpine.data('refreshController', () => ({
        interval: 30,
        paused: false,
        countdown: 30,
        _timer: null,

        init() {
            this.startTimer();
        },

        startTimer() {
            this.stopTimer();
            this.countdown = this.interval;
            this._timer = setInterval(() => {
                if (this.paused) return;
                this.countdown--;
                if (this.countdown <= 0) {
                    this.doRefresh();
                    this.countdown = this.interval;
                }
            }, 1000);
        },

        stopTimer() {
            if (this._timer) {
                clearInterval(this._timer);
                this._timer = null;
            }
        },

        togglePause() {
            this.paused = !this.paused;
        },

        setInterval(val) {
            this.interval = parseInt(val);
            this.startTimer();
        },

        doRefresh() {
            window.dispatchEvent(new CustomEvent('condora:refresh'));
        },

        refreshNow() {
            this.doRefresh();
            this.countdown = this.interval;
        },

        destroy() {
            this.stopTimer();
        },
    }));
});
