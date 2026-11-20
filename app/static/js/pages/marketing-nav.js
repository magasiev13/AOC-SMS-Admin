const mobileMenu = document.querySelector('.mobile-menu');

mobileMenu.addEventListener('keydown', event => {
    if (event.key === 'Escape' && mobileMenu.open) {
        mobileMenu.open = false;
        mobileMenu.querySelector('summary').focus();
    }
});
mobileMenu.addEventListener('focusout', event => {
    if (event.relatedTarget && !mobileMenu.contains(event.relatedTarget)) mobileMenu.open = false;
});
document.addEventListener('pointerdown', event => {
    if (mobileMenu.open && !mobileMenu.contains(event.target)) mobileMenu.open = false;
});
