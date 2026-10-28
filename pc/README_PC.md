# QC360 AI on your PC

All the browser checks run on your PC, so there is no 700 MB upload and no long wait. AI is used
only for the content review: spelling, grammar and the storyboard (SB).

## One-time set-up (about 30 minutes)

1. Install **Python 3.13** from https://www.python.org/downloads/
   On the first screen, tick **"Add python.exe to PATH"**.
2. Download the tester from GitHub (green **Code** button → **Download ZIP**):
   https://github.com/Avinashhub-novac/novac-tester-engine (branch `release-1.2.0`).
   Unzip it to a short path, for example `C:\eQC360\tester`.
3. Open the `pc` folder and double-click **setup.bat**. It installs everything else:
   - the Python packages
   - Google Chrome
   - LibreOffice (storyboards)
   - Poppler (SB page pictures)
   - FFmpeg (videos)

   When it finishes, it lists what it found.
   If something says MISSING, close the window and run setup.bat again.
   If winget is not available, install these by hand:
   - Google Chrome
   - LibreOffice – https://www.libreoffice.org
   - Poppler – https://github.com/oschwartz10612/poppler-windows/releases (unzip to `C:\poppler`)
   - FFmpeg – https://www.gyan.dev/ffmpeg/builds (unzip to `C:\ffmpeg`)

Keep Google Chrome on the same version for every test, so every report is made the same way.

## PCs without internet (offline kit)

1. On one PC **with** internet (Windows 10 or 11, with Python 3.13 installed), double-click **pc\make_offline_kit.bat**.
   It downloads into `pc\offline_kit` (about 700 MB):
   - the Python 3.13 installer
   - Google Chrome
   - LibreOffice
   - Poppler
   - FFmpeg
   - every Python package the tester needs
2. Copy the whole tester folder, including `pc\offline_kit`, to the other PCs (USB drive or network share).
3. On each PC, double-click **pc\setup.bat**. It finds the kit and installs everything from it, with no internet:
   - Python and the Python packages are installed for the user.
   - Poppler and FFmpeg are unpacked into `%LOCALAPPDATA%\QC360\tools`.
   - Google Chrome and LibreOffice are installed from their MSI files; Windows asks for administrator rights for these two.

If `make_offline_kit.bat` says winget is not available, it lists the download pages: save each installer into `pc\offline_kit`
by hand, then run it again for the Python packages.

## Test a course

1. In Explorer, select the course zip and its storyboard together. For SB and Alpha you can also add a style guide
   (image, Word or PDF): put **style** in its file name, e.g. `Client_Style_Guide.pdf`.
2. Drag them onto **pc\test.bat**.
3. Type the stage (`beta`, `alpha`, `gold`, `sb` or `custom`) or press Enter for Beta. Custom runs every check unless you
   name them with `--checks` (see Other options).

The window shows each step. The browser checks take about the course's running time plus 10 minutes.
Reports are saved in `Documents\..\eQC360\reports\<course name>_<date>` (your user folder → `eQC360\reports`).

## Content review – choose one

**A. Automatic (review key)**: the review runs by itself, in a few minutes.
Ask the site owner for the review key. Then, once, in a Command Prompt:

    setx QC360_REVIEW_KEY "<the key from the site owner>"

Close and reopen the window. Every test then finishes on its own.

**B. In the review chat (no key needed)**: at the end of the browser checks the window says:

1. Open the review chat (claude.ai) and start a new chat.
2. Attach `REVIEW_REQUEST.md` from the report folder and type:
   *Follow the instructions in this file and give me the JSON as a downloadable answers.json file.*
3. Save the **answers.json** it gives you into the same report folder.
4. Double-click **pc\finish.bat**. It finds the newest report waiting for its review.

## What you get

- **QC_Review_<course>.xlsx** – the team's QC report: issues with screenshots, Navigation Test, Assessment Test and
  LMS Completion.
- **SITE_IMPORT_<course>.zip** – to share the result, open the **QC360 AI PC** website
  (https://claude.ai/artifact/7frJK7iGTRTxEC4AeGbq6N), choose **Import PC report**, and pick this zip. The whole team then sees the report there, with
  its screenshots.

## Other options (Command Prompt, in the tester folder)

    python -m beta.local setup                                  check this PC
    python -m beta.local test course.zip SB.pptx --stage alpha  test with options
    python -m beta.local test course.zip --menu free --next free --seekbar free   (SOW: navigation not restricted)
    python -m beta.local test course.zip --stage custom --checks navigation,buttons,visual   only the checks you name (default: all)
    python -m beta.local test course.zip --next skip              do not check the Next restriction (also menu / seekbar)
    python -m beta.local test course.zip SB.pptx --stage sb --styleguide guide.pdf   test the SB against a style guide
    python -m beta.local test course.zip --no-swipe             skip the phone checks
    python -m beta.local finish "C:\Users\…\eQC360\reports\…"   finish a given report

Reports made on a PC may differ slightly from the website's: Google Chrome plays MP4 video, while the website's
cloud browser cannot.
