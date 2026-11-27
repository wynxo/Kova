---
name: Images
description: Draw pictures locally with generate_image when the user asks for an image, illustration, logo, wallpaper or photo-like picture.
---
Use this when the user asks you to draw, generate, create or imagine a picture, or runs /imagine.

Write the prompt for the image model, not for a person:

- Describe what is seen: the subject first, then the setting, then the style (photo, watercolour, 3D render, flat illustration), the lighting and the camera or composition.
- Be concrete: "a red fox curled up asleep on a mossy log in morning mist, soft golden backlight, shallow depth of field" beats "a nice fox picture".
- Keep it under about 60 words. Put text you want drawn in quotes, and keep it short.
- Pick a size only when the shape matters: 768x1024 for portrait, 1024x768 for landscape. Otherwise leave it out.

Call generate_image once. It takes seconds to a few minutes and the user is asked first. When it returns, show the picture with the Markdown line it gives you, unchanged, and say in one sentence what you drew. Offer one concrete change, such as a different style or angle.

If it fails because no image model is installed, tell the user to open Images in the Kova app (or the Marketplace) and pick the recommended model for their computer. Do not retry in a loop.

Never draw real, identifiable people in sexual or degrading situations, sexual content involving minors, or images meant to deceive (fake documents, fake news photos). Decline those plainly.
