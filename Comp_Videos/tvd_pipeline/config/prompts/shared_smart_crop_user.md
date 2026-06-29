Look at this image carefully. I need to crop it to a 9:16 portrait (vertical) format for a social media video.

Identify the FOCAL POINT — the most visually important and interesting region that should be preserved in the crop. Consider:
- People (especially faces, actions, interactions)
- Main subjects or products
- Key visual elements that tell the story
- Areas with the most visual interest or motion potential

Return the center coordinates of the most important region as normalized values:
- focus_x: 0.0 = left edge, 0.5 = center, 1.0 = right edge
- focus_y: 0.0 = top edge, 0.5 = center, 1.0 = bottom edge
- description: Brief description of what's at the focal point

Return your answer as a JSON object with exactly these three keys: focus_x, focus_y, description.

The crop window will be positioned around your focal point. For a wide landscape image being cropped to portrait, the horizontal position (focus_x) is most critical — it determines which vertical strip of the image is kept.