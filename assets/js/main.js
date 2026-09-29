/**
* Template Name: iPortfolio
* Template URL: https://bootstrapmade.com/iportfolio-bootstrap-portfolio-websites-template/
* Updated: Jun 29 2024 with Bootstrap v5.3.3
* Author: BootstrapMade.com
* License: https://bootstrapmade.com/license/
*/

(function() {
  "use strict";

  /**
   * Header toggle
   */
  const headerToggleBtn = document.querySelector('.header-toggle');

  function headerToggle() {
    document.querySelector('#header').classList.toggle('header-show');
    headerToggleBtn.classList.toggle('bi-list');
    headerToggleBtn.classList.toggle('bi-x');
  }
  headerToggleBtn.addEventListener('click', headerToggle);

  /**
   * Hide mobile nav on same-page/hash links
   */
  document.querySelectorAll('#navmenu a').forEach(navmenu => {
    navmenu.addEventListener('click', () => {
      if (document.querySelector('.header-show')) {
        headerToggle();
      }
    });

  });

  /**
   * Toggle mobile nav dropdowns
   */
  document.querySelectorAll('.navmenu .toggle-dropdown').forEach(navmenu => {
    navmenu.addEventListener('click', function(e) {
      e.preventDefault();
      this.parentNode.classList.toggle('active');
      this.parentNode.nextElementSibling.classList.toggle('dropdown-active');
      e.stopImmediatePropagation();
    });
  });

  /**
   * Preloader
   */
  const preloader = document.querySelector('#preloader');
  if (preloader) {
    window.addEventListener('load', () => {
      preloader.remove();
    });
  }

  /**
   * Scroll top button
   */
  let scrollTop = document.querySelector('.scroll-top');

  function toggleScrollTop() {
    if (scrollTop) {
      window.scrollY > 100 ? scrollTop.classList.add('active') : scrollTop.classList.remove('active');
    }
  }
  scrollTop.addEventListener('click', (e) => {
    e.preventDefault();
    window.scrollTo({
      top: 0,
      behavior: 'smooth'
    });
  });

  window.addEventListener('load', toggleScrollTop);
  document.addEventListener('scroll', toggleScrollTop);

  /**
   * Animation on scroll function and init
   */
  function aosInit() {
    AOS.init({
      duration: 600,
      easing: 'ease-in-out',
      once: true,
      mirror: false
    });
  }
  window.addEventListener('load', aosInit);

  /**
   * Init typed.js
   */
  const selectTyped = document.querySelector('.typed');
  if (selectTyped) {
    let typed_strings = selectTyped.getAttribute('data-typed-items');
    typed_strings = typed_strings.split(',');
    new Typed('.typed', {
      strings: typed_strings,
      loop: true,
      typeSpeed: 100,
      backSpeed: 50,
      backDelay: 2000
    });
  }

  /**
   * Initiate Pure Counter
   */
  new PureCounter();

  /**
   * Animate the skills items on reveal
   */
  let skillsAnimation = document.querySelectorAll('.skills-animation');
  skillsAnimation.forEach((item) => {
    new Waypoint({
      element: item,
      offset: '80%',
      handler: function(direction) {
        let progress = item.querySelectorAll('.progress .progress-bar');
        progress.forEach(el => {
          el.style.width = el.getAttribute('aria-valuenow') + '%';
        });
      }
    });
  });

  /**
   * Initiate glightbox
   */
  const glightbox = GLightbox({
    selector: '.glightbox'
  });

  /**
   * Init isotope layout and filters
   */
  document.querySelectorAll('.isotope-layout').forEach(function(isotopeItem) {
    let layout = isotopeItem.getAttribute('data-layout') ?? 'masonry';
    let filter = isotopeItem.getAttribute('data-default-filter') ?? '*';
    let sort = isotopeItem.getAttribute('data-sort') ?? 'original-order';

    let initIsotope;
    imagesLoaded(isotopeItem.querySelector('.isotope-container'), function() {
      initIsotope = new Isotope(isotopeItem.querySelector('.isotope-container'), {
        itemSelector: '.isotope-item',
        layoutMode: layout,
        filter: filter,
        sortBy: sort
      });
    });

    isotopeItem.querySelectorAll('.isotope-filters li').forEach(function(filters) {
      filters.addEventListener('click', function() {
        isotopeItem.querySelector('.isotope-filters .filter-active').classList.remove('filter-active');
        this.classList.add('filter-active');
        initIsotope.arrange({
          filter: this.getAttribute('data-filter')
        });
        if (typeof aosInit === 'function') {
          aosInit();
        }
      }, false);
    });

  });

  /**
   * Init swiper sliders
   */
  function initSwiper() {
    document.querySelectorAll(".init-swiper").forEach(function(swiperElement) {
      let config = JSON.parse(
        swiperElement.querySelector(".swiper-config").innerHTML.trim()
      );

      if (swiperElement.classList.contains("swiper-tab")) {
        initSwiperWithCustomPagination(swiperElement, config);
      } else {
        new Swiper(swiperElement, config);
      }
    });
  }

  window.addEventListener("load", initSwiper);

  /**
   * Correct scrolling position upon page load for URLs containing hash links.
   */
  window.addEventListener('load', function(e) {
    if (window.location.hash) {
      if (document.querySelector(window.location.hash)) {
        setTimeout(() => {
          let section = document.querySelector(window.location.hash);
          let scrollMarginTop = getComputedStyle(section).scrollMarginTop;
          window.scrollTo({
            top: section.offsetTop - parseInt(scrollMarginTop),
            behavior: 'smooth'
          });
        }, 100);
      }
    }
  });

  /**
   * Navmenu Scrollspy
   */
  let navmenulinks = document.querySelectorAll('.navmenu a');

  function navmenuScrollspy() {
    navmenulinks.forEach(navmenulink => {
      if (!navmenulink.hash) return;
      let section = document.querySelector(navmenulink.hash);
      if (!section) return;
      let position = window.scrollY + 200;
      if (position >= section.offsetTop && position <= (section.offsetTop + section.offsetHeight)) {
        document.querySelectorAll('.navmenu a.active').forEach(link => link.classList.remove('active'));
        navmenulink.classList.add('active');
      } else {
        navmenulink.classList.remove('active');
      }
    })
  }
  window.addEventListener('load', navmenuScrollspy);
  document.addEventListener('scroll', navmenuScrollspy);


  /**
   * Project modals - gestion locale pour éviter les conflits avec Bootstrap.
   * Les boutons PDF restent toujours cliquables dans le contenu du modal.
   */
  function initProjectModals() {
    const modals = document.querySelectorAll('.project-modal');
    if (!modals.length) return;

    let activeModal = null;
    let backdrop = null;

    function closeProjectModal() {
      if (!activeModal) return;

      activeModal.classList.remove('is-open', 'show');
      activeModal.setAttribute('aria-hidden', 'true');
      activeModal.style.display = 'none';

      if (backdrop) {
        backdrop.remove();
        backdrop = null;
      }

      document.body.classList.remove('modal-open');
      document.body.style.removeProperty('padding-right');
      document.body.style.removeProperty('overflow');
      activeModal = null;
    }

    function openProjectModal(modal) {
      if (activeModal) closeProjectModal();

      activeModal = modal;
      modal.classList.add('is-open', 'show');
      modal.setAttribute('aria-hidden', 'false');
      modal.style.display = 'block';

      backdrop = document.createElement('div');
      backdrop.className = 'modal-backdrop fade show project-modal-backdrop';
      backdrop.addEventListener('click', closeProjectModal);
      document.body.appendChild(backdrop);

      document.body.classList.add('modal-open');
      document.body.style.overflow = 'hidden';

      const closeButton = modal.querySelector('[data-bs-dismiss="modal"]');
      if (closeButton) closeButton.focus();
    }

    document.querySelectorAll('.btn-voir-plus').forEach(button => {
      button.addEventListener('click', function(event) {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();

        const targetId = this.getAttribute('data-bs-target');
        const modal = targetId ? document.querySelector(targetId) : null;
        if (modal) openProjectModal(modal);
      }, true);
    });

    modals.forEach(modal => {
      modal.addEventListener('click', function(event) {
        const closeButton = event.target.closest('[data-bs-dismiss="modal"]');
        if (closeButton) {
          event.preventDefault();
          event.stopPropagation();
          closeProjectModal();
          return;
        }

        // Clic sur le fond du modal : fermeture.
        if (event.target === modal) {
          closeProjectModal();
        }
      }, true);

      // Empêche le fond de recevoir les clics provenant du contenu.
      const dialog = modal.querySelector('.modal-dialog');
      if (dialog) {
        dialog.addEventListener('click', event => event.stopPropagation());
      }
    });

    // Les liens PDF doivent rester des liens normaux.
    document.querySelectorAll('.project-modal .cr-download').forEach(link => {
      link.addEventListener('click', event => {
        event.stopPropagation();
      });
    });

    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && activeModal) {
        closeProjectModal();
      }
    });
  }

  window.addEventListener('load', initProjectModals);

})();