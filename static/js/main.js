
function installActionDelegation() {
    document.addEventListener('click', (event) => {
        const target = event.target.closest('[data-action], [data-set-input]');
        if (!target) return;
        if (target.dataset.setInput) {
            const input = document.getElementById(target.dataset.setInput);
            if (input) {
                input.value = target.dataset.setValue || '';
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.focus();
            }
            return;
        }
        const action = target.dataset.action;
        if (!action) return;
        const fn = window[action];
        if (typeof fn !== 'function') return;
        const arg = target.dataset.actionArg;
        if (arg === undefined) {
            fn();
        } else if (/^-?\d+(?:\.\d+)?$/.test(arg)) {
            fn(Number(arg));
        } else {
            fn(arg);
        }
    });
}

/** Paisa Wallet public and dashboard interactions. */
document.addEventListener('DOMContentLoaded', () => {
    installActionDelegation();
    console.log('Paisa Wallet frontend loaded.');
});
