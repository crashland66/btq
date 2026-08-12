(() => {
  "use strict";

  const photoLinks = document.querySelectorAll("a.media-frame");
  const linkedPhotos = Array.from(photoLinks).filter((link) => link.querySelector("img"));
  if (!linkedPhotos.length) {
    return;
  }

  const lightbox = document.createElement("div");
  lightbox.className = "photo-lightbox";
  lightbox.hidden = true;
  lightbox.setAttribute("role", "dialog");
  lightbox.setAttribute("aria-modal", "true");
  lightbox.setAttribute("aria-label", "Enlarged photo");

  const enlargedPhoto = document.createElement("img");
  const closeButton = document.createElement("button");
  closeButton.className = "lightbox-close";
  closeButton.type = "button";
  closeButton.textContent = "Close";

  lightbox.append(enlargedPhoto, closeButton);
  document.body.append(lightbox);

  let originatingLink = null;

  const dismiss = () => {
    if (lightbox.hidden) {
      return;
    }
    lightbox.hidden = true;
    document.body.classList.remove("lightbox-open");
    if (originatingLink && document.contains(originatingLink)) {
      originatingLink.focus();
    }
    originatingLink = null;
  };

  linkedPhotos.forEach((link) => {
    link.addEventListener("click", (event) => {
      const photo = link.querySelector("img");
      if (!photo) {
        return;
      }
      event.preventDefault();
      originatingLink = link;
      enlargedPhoto.src = photo.getAttribute("src") || link.href;
      enlargedPhoto.alt = photo.getAttribute("alt") || "";
      lightbox.hidden = false;
      document.body.classList.add("lightbox-open");
      closeButton.focus();
    });
  });

  closeButton.addEventListener("click", dismiss);
  lightbox.addEventListener("click", (event) => {
    if (event.target !== enlargedPhoto) {
      dismiss();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (!lightbox.hidden && event.key === "Escape") {
      event.preventDefault();
      dismiss();
    }
  });
})();
