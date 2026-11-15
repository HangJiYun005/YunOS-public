# YunOS v8.2-D Visual Viewport Design QA

**Source visual truth**

- Home state: `E:\Codex\codex-remote-attachments\019f857b-208d-7470-a1b1-eaee774d731c\D6F349B7-D1D3-4F64-91BE-D52153733920\1-照片-1.jpg`
- Focus failure state: `E:\Codex\codex-remote-attachments\019f857b-208d-7470-a1b1-eaee774d731c\D6F349B7-D1D3-4F64-91BE-D52153733920\2-照片-2.jpg`
- Keyboard-dismiss reset failure: `E:\Codex\codex-remote-attachments\019f857b-208d-7470-a1b1-eaee774d731c\367DF40D-43A9-46FC-9D67-2576A6127101\1-照片-1.jpg`

**Implementation evidence**

- Home: `E:\Codex\visualizations\2026\07\21\019f857b-208d-7470-a1b1-eaee774d731c\v82-home-430x932.png`
- Focused visual viewport: `E:\Codex\visualizations\2026\07\21\019f857b-208d-7470-a1b1-eaee774d731c\v82-focused-visual-402x520.png`
- Full focused layout viewport: `E:\Codex\visualizations\2026\07\21\019f857b-208d-7470-a1b1-eaee774d731c\v82-focused-full-430x932.png`
- Combined comparison: `E:\Codex\visualizations\2026\07\21\019f857b-208d-7470-a1b1-eaee774d731c\v82-visual-comparison.png`

**Viewport and normalization**

- Source screenshots: 590 × 1280 pixels; Safari/device density is not available from the supplied JPEGs.
- Implementation home: 430 × 932 CSS pixels at device scale factor 1.
- Focus simulation: layout viewport 430 × 932; visual viewport 402 × 520 at offset left 14 and offset top 300; device scale factor 1.
- The combined comparison crops browser-owned status, address, and keyboard chrome where necessary and compares app-owned content. Resizing is aspect-preserving.

**State and interaction evidence**

- The unchanged home state keeps the header and centered search composition.
- The focused state keeps the YunOS/最近 header and search field entirely inside the simulated visual viewport.
- When the simulated keyboard closes while Safari-style input focus remains active, the empty search field returns to the centered home position; reopening the keyboard pins it below the header again.
- Browser assertions recorded Shell bounds `(14, 300)–(416, 820)`, header vertical bounds `300–358`, and search bounds `358–412`.
- Page scroll remained `(0, 0)`; no `window.scrollTo` behavior was introduced.
- Portrait, low-height landscape, desktop, search, pagination, reader, return restoration, and GET-only checks passed without console or page errors.

**Findings**

- No actionable P0, P1, or P2 mismatch remains.
- Fonts and typography: existing families, weights, sizes, hierarchy, and copy are unchanged. Expected Safari-versus-Chromium rasterization differences are non-actionable.
- Spacing and layout rhythm: the home composition is preserved; focused search is now directly below the existing header without overlap or overflow.
- Colors and visual tokens: existing canvas, border, surface, shadow, focus, and text tokens are unchanged.
- Image quality and asset fidelity: the screen contains no product imagery or newly introduced visual assets.
- Copy and content: YunOS, 最近, and 搜索记忆 remain unchanged.

**Comparison history**

- Earlier P1: after iPhone Safari focus, the app header and search field moved above the visible area.
- Fix: synchronize visual viewport width, height, and offsets; bind the existing fixed Shell to those values; resynchronize immediately and on the next animation frame after focus.
- Post-fix evidence: focused implementation screenshot and browser geometry show the header and search field fully visible inside the visual viewport.
- Second P1: iPhone Safari could hide the keyboard without blurring the search input, leaving the empty search field pinned below the header.
- Fix: distinguish retained input focus from an open keyboard by recording the keyboard open-to-closed transition; preserve top placement for queries, results, low-height layouts, and a reopened keyboard.
- Post-fix evidence: the retained-focus regression records `keyboardOpen=false`, `searchPosition=center`, and form `y=355.16`; reopening records `keyboardOpen=true`, `searchPosition=top`, and a 58 px offset below the visual viewport top.

**Open questions**

- None. The second iPhone Safari 5G test confirmed that the empty search field
  returns to the centered home position after the keyboard is dismissed and
  pins below the header again when the keyboard reopens.

**Implementation checklist**

- [x] Preserve existing layout and 220 ms search transform.
- [x] Synchronize resize and scroll visual viewport changes.
- [x] Avoid active page scrolling and new fixed-delay compensation.
- [x] Upgrade app-shell resource and cache versions.
- [x] Run static, browser, responsive, reader, and GET-only regression checks.

final result: passed
