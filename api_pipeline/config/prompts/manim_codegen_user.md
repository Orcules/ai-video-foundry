Generate a Manim Community v0.19.0 Python script for the following animated explainer.

<video_data>
Description:
{description}

Target duration (seconds): {duration_target}
</video_data>

Constraints recap (re-read before answering — ALL must hold):
- Output a COMPLETE, self-contained Python file. Not a snippet, not a diff, not a partial class. The file must be syntactically valid Python on its own and ready to save to disk and run unmodified.
- The file MUST define exactly one class named `ExplainerScene` that inherits from `Scene` (i.e. `class ExplainerScene(Scene):`). This Scene subclass is REQUIRED — the renderer invokes it by name and fails with "No scenes were rendered" if it is missing or renamed.
- The class MUST define a `construct(self)` method containing the full animation body. Do not leave it as `pass` or a stub.
- First line of the file: `from manim import *`. The only other allowed import is `import numpy as np` (include only if `np.` is actually used).
- Output raw Python source ONLY. No markdown fences (no ```python, no ```), no prose, no leading commentary, no trailing remarks, no `if __name__ == "__main__"` block. The very first character of your reply MUST be the `f` of `from manim import *`. The very last character MUST be the end of a Python statement.
- Do NOT truncate. Finish every `self.play(...)` / `self.wait(...)` call and close every bracket and indent block. If you are running low on budget, prefer fewer animations over an incomplete file.
- Prefer `Text(...)` over `MathTex(...)` unless the description explicitly requires a typeset equation.
- Keep all mobjects inside the 16:9 frame (|x| <= 6.5, |y| <= 3.7).
- Total animation time (sum of `run_time` + `self.wait(...)` durations) should be roughly {duration_target} seconds (±20%).
- End the `construct` method with `self.wait(1)` as the final statement.

Begin the complete Python file now.
