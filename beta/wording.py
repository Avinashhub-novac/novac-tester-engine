"""Plain-language wording for the review report.

Every issue is rewritten here before the report is built, so reviewers read
"Next button not working" rather than tool language. Technical terms are kept
only where they help find the problem (a file name, an exact quote).
"""
import re

# (pattern, replacement message, short label for the screenshot tag)
RULES = [
    (r"^Not translated (?:into \w+ )?in (.+?): “(.+)”$", lambda m: f"English text left untranslated in {_where(m.group(1))}: “{m.group(2)}”", "Not translated"),
    # navigation
    (r"^Missing file \((\d+)\)$", "A file is missing from the course (it does not load)", "File missing"),
    (r"^Script error.*$", "The course shows an error while running", "Error on this screen"),
    (r"^Next loops back to a slide that was already visited$", "Next button goes back to an earlier screen", "Next goes back"),
    (r"^No way forward on this slide.*$", "No way to go to the next screen", "Can't go forward"),
    (r"^No Next button on this slide.*$", "No way to go to the next screen", "Can't go forward"),
    (r"^“(.+)” did not move to another slide$", r"“\1” button not working", r"“\1” not working"),
    (r"^“(.+)” did not move back$", r"Back button not working: “\1” does not open the previous screen", "Back not working"),
    (r"^“(.+)” went to the wrong slide$", r"“\1” button opens the wrong screen", "Opens wrong screen"),
    (r"^Next button does nothing on this screen; the learner has to pick one of the choices$", "Next button does nothing on this screen (only the choices on the screen move on)", "Next does nothing"),
    (r"^Next button did not move to another slide$", "Next button not working", "Next not working"),
    (r"^Next went to the wrong slide$", "Next button opens the wrong screen", "Next opens wrong screen"),
    (r"^Next stays locked after the slide finished$", "Next button stays disabled after the screen finishes", "Next stays disabled"),
    (r"^Slide is skipped: (.+) goes straight to (.+)$", r"Screen is skipped: Next on \1 goes straight to \2", "Screen skipped"),
    (r"^Slide not reached by Next.*$", "This screen cannot be reached with Next", "Screen not reachable"),
    (r"^No Prev button on this slide$", "No Back button on this screen", "No Back button"),
    (r"^Prev button did not move back$", "Back button not working: the player's PREV button does not open the previous screen", "Back not working"),
    (r"^Prev went to a different slide than the one before it$", "Back button opens the wrong screen (not the previous screen)", "Back opens wrong screen"),
    (r"^Time limit reached during the (\w+) pass.*$", "Testing stopped early because the course is very long – check the remaining screens by hand", "Not fully tested"),
    (r"^The (\w+) pass (timed out|stopped early)$", "Part of the testing could not finish – check this area by hand", "Not fully tested"),
    (r"^html5/data/js/data\.js not found.*$", "Course files could not be read fully – some checks rely only on what is seen on screen", "Course files"),
    (r"^data\.js could not be decoded\.$", "Course files could not be read fully", "Course files"),
    # menu
    (r"^Menu item not marked visited after the slide was viewed$", "Menu does not show this screen as visited", "Not marked visited"),
    (r"^Current slide is not highlighted in the menu$", "Menu does not highlight the current screen", "Not highlighted"),
    (r"^Menu item does not match any slide title$", "Menu title does not match any screen title", "Menu title mismatch"),
    (r"^Menu title differs from the screen title$", "Menu title is different from the screen title", "Menu title differs"),
    (r"^Slide is not listed in the menu$", "This screen is not listed in the menu", "Not in menu"),
    (r"^Menu item did not open a recognisable slide$", "Menu item not working", "Menu item not working"),
    (r"^Menu item opened the wrong slide$", "Menu item opens the wrong screen", "Opens wrong screen"),
    (r"^Menu navigation looks restricted.*$", "Menu items cannot be opened (menu is locked)", "Menu locked"),
    # buttons
    (r"^Player button '(.+)' could not be clicked$", r"\1 button cannot be clicked", r"\1 can't be clicked"),
    (r"^Player button '(.+)' does nothing when clicked$", r"\1 button not working", r"\1 not working"),
    (r"^Object has a click trigger with no action$", "Button not working (nothing happens on click)", "Button not working"),
    (r"^Clickable object does nothing when clicked$", "Button not working (nothing happens on click)", "Button not working"),
    # mobile
    (r"^Swipe left moved to the next slide while Next was locked$", "Swiping skips the screen before it finishes", "Swipe skips screen"),
    (r"^Swipe right moved back while Prev was locked$", "Swiping back works even though Back is disabled", "Swipe back not locked"),
    (r"^Swipe left did not go to the next slide.*$", "Swiping does not go to the next screen", "Swipe not working"),
    (r"^Swipe right did not go back.*$", "Swiping does not go back to the previous screen", "Swipe back not working"),
    (r"^Swipe right went to an unexpected slide$", "Swiping back opens the wrong screen", "Swipe opens wrong screen"),
    (r"^Swipe moved to another slide although the player shows no Next button$", "Swiping skips to another screen", "Swipe skips screen"),
    (r"^Swipe next \(left\) moves on while Next is locked$", "Swipe next (swipe left) skips the screen before it finishes (Next is still locked)", "Swipe next skips"),
    (r"^Swipe next \(left\) does not go to the next screen.*$", "Swipe next (swipe left) does not work: it does not open the next screen", "Swipe next not working"),
    (r"^Swipe next \(left\) opens the wrong screen$", "Swipe next (swipe left) opens the wrong screen", "Swipe next wrong screen"),
    (r"^Swipe previous \(right\) does not go back.*$", "Swipe previous (swipe right) does not work: it does not open the previous screen", "Swipe back not working"),
    (r"^Swipe previous \(right\) opens the wrong screen$", "Swipe previous (swipe right) opens the wrong screen", "Swipe back wrong screen"),
    (r"^Swipe previous \(right\) goes back although Back is locked$", "Swipe previous (swipe right) goes back although Back is locked", "Swipe back not locked"),
    # seekbar
    (r"^Empty seekbar.*$", "Seekbar is shown on a screen with no audio or video", "Empty seekbar"),
    (r"^Seekbar did not move while the slide played$", "Seekbar does not move while the screen plays", "Seekbar not moving"),
    # screen numbers
    (r"^Wrong total in screen number: '(.+)'$", r"Wrong total in screen number “\1”", "Wrong screen total"),
    (r"^Duplicate screen number (\d+)$", r"Screen number \1 is repeated", "Repeated screen number"),
    (r"^Screen number out of order: (.+)$", r"Screen number out of order: “\1”", "Wrong screen number"),
    (r"^Slide has no screen number while the slides around it do$", "Screen number missing", "Screen number missing"),
    (r"^First numbered slide starts at (\d+), not 1$", r"Screen numbers start at \1 instead of 1", "Wrong screen number"),
    (r"^Screen total says (\d+) but (\d+) slides carry.*$", r"Screen total says \1, but \2 screens are numbered", "Wrong screen total"),
    # completion
    (r"^The course never connected to the LMS.*$", "Course does not report progress to the LMS", "No LMS tracking"),
    (r"^Course did not report completion after the last slide.*$", "Course does not show as completed after the last screen", "Not completed"),
    # restrictions
    (r"^Menu is not restricted, but it should be restricted.*$", "Menu is not locked: screens not yet visited can be opened (should be locked as per SOW)", "Menu not locked"),
    (r"^Menu is restricted, but it should be free.*$", "Menu is locked, but it should be open as per SOW", "Menu locked"),
    (r"^Seekbar is not restricted, but it should be restricted.*$", "Seekbar can be dragged ahead (should be locked as per SOW)", "Seekbar not locked"),
    (r"^Seekbar is restricted, but it should be free.*$", "Seekbar cannot be dragged, but it should be free as per SOW", "Seekbar locked"),
    (r"^Next button is not restricted, but it should be restricted.*$", "Next button works before the screen finishes (should be locked as per SOW)", "Next not locked"),
    (r"^Next button is restricted, but it should be free.*$", "Next button is locked, but it should be free as per SOW", "Next locked"),
    # transcript / captions
    (r"^Slide has narration but no closed captions were found$", "Captions missing on this screen", "Captions missing"),
    (r"^Closed captions differ from the transcript$", "Captions do not match the transcript", "Captions don't match"),
    (r"^Captions differ from narration.*$", "Captions do not match the narration", "Captions don't match"),
    (r"^Speech with no caption$", "Captions missing for this part of the video", "Captions missing"),
    (r"^Narration has no caption$", "Captions missing for this part of the video", "Captions missing"),
    (r"^Caption is shown while nothing is being said$", "Caption shown when no one is speaking", "Caption out of sync"),
    (r"^Captions run past the end of the video$", "Captions continue after the video ends", "Captions out of sync"),
    (r"^No captions found.*$", "No captions found for the video", "No captions"),
    # language
    (r"^(Spelling|Grammar|Punctuation) in (slide title|on-screen text|closed captions|captions|transcript|notes): “(.+)”$",
     lambda m: f"{m.group(1)} mistake in {_where(m.group(2))}: “{m.group(3)}”", lambda m: f"{m.group(1)} mistake"),
    # storyboard
    (r"^Storyboard not uploaded.*$", "Storyboard not uploaded, so the content was not matched with the SB", "No storyboard"),
    (r"^Not as per SB – (.+)$", r"Not as per SB: \1", "Not as per SB"),
    # SB stage: the issue text is already plain; the label on the picture is its first words
    (r"^SB: (.+)$", r"SB: \1", lambda m: (lambda t: t if len(t) <= 34 else t[:32].rstrip() + "…")(m.group(1))),
    # video
    (r"^Black screen for ([\d.]+)s$", lambda m: f"Black screen for {round(float(m.group(1)))} seconds", "Black screen"),
    (r"^Picture frozen for ([\d.]+)s$", lambda m: f"Video freezes for {round(float(m.group(1)))} seconds", "Video freezes"),
    (r"^The file has no video stream$", "Video does not play (no picture)", "No picture"),
]


def _where(src):
    return {"slide title": "the screen title", "on-screen text": "on-screen text", "closed captions": "captions",
            "captions": "captions", "transcript": "the transcript", "notes": "the transcript"}.get(src, src)


def _apply(rep, m):
    return rep(m) if callable(rep) else m.expand(rep)


def plain(message):
    """Returns (plain message, short screenshot label)."""
    for pat, rep, tag in RULES:
        m = re.match(pat, message)
        if m:
            return _apply(rep, m), _apply(tag, m)
    return message, message if len(message) <= 40 else message[:38].rstrip() + "…"


DETAIL_RULES = [
    (r"^Expected (.+), got (.+)$", r"Should open: \1\nOpens: \2"),
    (r"^Moved to (.+)$", r"Went to: \1"),
    (r"^Went to (.+)$", r"Went to: \1"),
    (r"^Opened (.+)$", r"Opened: \1"),
    (r"^Also on (.+)$", r"Also on: \1"),
    (r"^The learner may need to interact first\..*$", "The learner may be stuck unless they click something on the screen."),
    (r"^Check that this is an intended branch\.$", "Check if this is an intended branch."),
    (r"^story_content/.+$", None),
    (r"^.*\.(png|jpe?g|gif|svg)$", None),   # picture file names
    (r"^(DIV|SPAN|SVG|G|IMG|PATH|CANVAS|BUTTON|A)$", None),
    (r"^(?=.*\d)(?=.*[A-Za-z])[0-9A-Za-z]{11}$", None),   # Storyline's internal object ids   # bare page-element names mean nothing to a reviewer   # internal audio paths are not useful to reviewers
]


def plain_detail(detail):
    out = []
    for line in (detail or "").split("\n"):
        for pat, rep in DETAIL_RULES:
            m = re.match(pat, line)
            if m:
                line = None if rep is None else m.expand(rep)
                break
        if line:
            out.append(line)
    return "\n".join(out)


def apply(findings):
    """Rewrites messages in place and adds a short `tag` used on the screenshot."""
    for f in findings:
        if f.get("plain"):
            continue
        msg, tag = plain(f.get("message", ""))
        f["message"], f["tag"], f["detail"], f["plain"] = msg, tag, plain_detail(f.get("detail", "")), True
    return findings


# What needs to be done, when the check itself did not say (matched on the plain message)
FIXES = [
    (r"cannot go past this screen", "Make the way forward work on this screen, then send the course for testing again."),
    (r"^Next button (not working|opens the wrong screen|goes back)", "Make Next open the next screen as per the SB flow."),
    (r"^Next button does nothing", "Make Next work on this screen, or hide it when the learner must pick one of the choices."),
    (r"^Mobile testing could not go forward", "Make the screen's own forward button work on a phone."),
    (r"^“.+” (button )?not working", "Make this button work (it should open its screen or its content)."),
    (r"button opens the wrong screen", "Make the button open the correct screen as per the SB flow."),
    (r"^Back button", "Make the Back button open the previous screen."),
    (r"^No Back button", "Add a Back button, or confirm with the ID that it is not needed here."),
    (r"Next button stays disabled", "Enable Next once the screen has finished and every item has been opened."),
    (r"^Screen is skipped", "Make Next open every screen in order, or confirm the branch with the ID."),
    (r"cannot be reached with Next", "Link this screen into the Next path, or confirm the branch with the ID."),
    (r"^Menu does not show this screen as visited", "Mark the menu item as visited once the screen is viewed."),
    (r"^Menu does not highlight", "Highlight the current screen in the menu."),
    (r"^Menu title", "Use the same title in the menu and on the screen."),
    (r"not listed in the menu", "Add this screen to the menu."),
    (r"^Menu item", "Make the menu item open its own screen."),
    (r"Menu items cannot be opened", "Unlock the menu, or confirm with the SOW that it should be locked."),
    (r"Button not working|button not working|buttons not working", "Make the button work, or remove it if it is not needed."),
    (r"Swipe next", "Make swipe left on a phone behave exactly like the Next button."),
    (r"Swipe previous", "Make swipe right on a phone behave exactly like the Back button."),
    (r"^Swipe next.*skips", "Keep swipe next locked until the screen has finished, like the Next button."),
    (r"^Swipe next", "Make swipe left on a phone open the next screen, like the Next button."),
    (r"^Swipe previous.*although Back is locked", "Keep swipe previous locked when the Back button is locked."),
    (r"^Swipe previous", "Make swipe right on a phone open the previous screen, like the Back button."),
    (r"Swiping", "Make swiping on a phone behave like Next / Back."),
    (r"^Seekbar is shown on a screen with no audio", "Hide the seekbar on this screen, or add the missing audio."),
    (r"^Seekbar does not move", "Make the seekbar follow the audio."),
    (r"^Wrong total in screen number", "Use the same screen total on every screen."),
    (r"used for screens with different titles", "Give each screen with a different title its own number, in order."),
    (r"^Screen number out of order", "Number the screens in order (1, 2, 3 …)."),
    (r"^Screen number missing", "Add the screen number, in the same style as the other screens."),
    (r"^Screen numbers start at", "Start the screen numbers at 1."),
    (r"^Screen total says", "Make the total match the number of numbered screens."),
    (r"does not report progress to the LMS", "Publish for LMS (SCORM) with tracking switched on, and upload that zip."),
    (r"does not show as completed", "Check the tracking settings (Publish → LMS) and the completion trigger."),
    (r"(locked|open) as per SOW|should be (locked|free|open) as per SOW", "Set this as the SOW says."),
    (r"^Captions missing", "Add closed captions for the narration."),
    (r"^Captions do not match", "Make the captions match the narration / transcript word for word."),
    (r"Caption shown when no one is speaking|Captions continue after", "Fix the caption timing."),
    (r"^Spelling mistake", "Correct the spelling."),
    (r"^Grammar mistake", "Correct the grammar."),
    (r"^Punctuation mistake", "Correct the punctuation."),
    (r"^Not as per SB", "Change the course to match the SB."),
    (r"^Storyboard not uploaded", "Upload the course again together with its storyboard."),
    (r"appears more than once|repeated|[Dd]uplicate", "Remove the repeated text."),
    (r"^Ordinal ending is not in superscript", "Set the ordinal ending (st, nd, rd, th) in superscript."),
    (r"[Mm]issing space", "Add the missing space."),
    (r"[Dd]ouble space|extra space", "Remove the extra space."),
    (r"Pop-up does not close", "Make the Close button close the pop-up."),
    (r"Text overlaps other text", "Move or resize the text boxes so they do not cover each other."),
    (r"Next is enabled before all clickable items", "Keep Next disabled until every clickable item has been opened."),
    (r"Next stays disabled after all clickable items", "Enable Next once every clickable item has been opened."),
    (r"^Answer option cannot be selected", "Make this option clickable so the learner can choose it."),
    (r"[Rr]esults screen", "Check the results screen and the quiz settings so the result is shown and reported correctly."),
    (r"Black screen|Video freezes|no picture", "Fix the video at this point."),
    (r"course shows an error", "Fix the error on this screen (it may stop the screen working)."),
    (r"file is missing", "Add the missing file and publish again."),
]


def default_fix(message):
    for pat, fix in FIXES:
        if re.search(pat, message or ""):
            return fix
    return "Correct this and check the screen again."
