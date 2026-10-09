// Thumb wheel -> Ctrl+Up ("up") or Ctrl+Down ("down"), as Logi Options+ sent.
//
// A native binary, not osascript: OpenLogi waits for each command to exit, and
// osascript launched from its agent spends ~2.5s in per-launch security checks.
// OpenLogi's own CustomShortcut can't be used because it omits the fn flag
// macOS requires to match arrow-key hotkeys like Mission Control. A single
// flick produces many wheel steps, so fire once per cooldown.
//
// setup.sh builds this into ~/.config/openlogi/bin/thumbwheel-key on macOS.

#include <ApplicationServices/ApplicationServices.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

static const double kCooldownSeconds = 0.5;
static const CGKeyCode kLeftControl = 59;

static double now_seconds(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec + tv.tv_usec / 1e6;
}

static int within_cooldown(const char *stamp_path, double now) {
    FILE *f = fopen(stamp_path, "r");
    if (!f) return 0;
    double last = 0;
    int read = fscanf(f, "%lf", &last);
    fclose(f);
    return read == 1 && now - last < kCooldownSeconds;
}

static void record_fire(const char *stamp_path, double now) {
    FILE *f = fopen(stamp_path, "w");
    if (!f) return;
    fprintf(f, "%f\n", now);
    fclose(f);
}

static void post_key(CGEventSourceRef source, CGKeyCode key, bool down, CGEventFlags flags) {
    CGEventRef event = CGEventCreateKeyboardEvent(source, key, down);
    CGEventSetFlags(event, flags);
    CGEventPost(kCGHIDEventTap, event);
    CFRelease(event);
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    CGKeyCode key;
    if (strcmp(argv[1], "up") == 0) key = 126;
    else if (strcmp(argv[1], "down") == 0) key = 125;
    else return 2;

    const char *tmp = getenv("TMPDIR");
    char stamp_path[1024];
    snprintf(stamp_path, sizeof stamp_path, "%s/openlogi-thumbwheel.last", tmp ? tmp : "/tmp");

    double now = now_seconds();
    if (within_cooldown(stamp_path, now)) return 0;
    record_fire(stamp_path, now);

    // Ctrl+fn exactly: the default Mission Control and App Expose hotkeys are
    // stored with these two modifiers, and an extra flag can stop a match.
    CGEventFlags arrow_flags = kCGEventFlagMaskControl | kCGEventFlagMaskSecondaryFn;
    CGEventSourceRef source = CGEventSourceCreate(kCGEventSourceStateHIDSystemState);

    // Press and release Ctrl itself around the arrow, as a keyboard would.
    // Once Mission Control is open it reads the held-modifier state rather than
    // the arrow event's flags, so a bare flagged arrow can open it but not close it.
    post_key(source, kLeftControl, true, kCGEventFlagMaskControl);
    post_key(source, key, true, arrow_flags);
    post_key(source, key, false, arrow_flags);
    post_key(source, kLeftControl, false, 0);

    if (source) CFRelease(source);
    return 0;
}
