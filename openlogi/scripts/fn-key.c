// Forward button -> fn (Globe) taps for Wispr Flow's hands-free mode:
// double-tap fn starts dictation, a single tap ends it.
//
// OpenLogi's CustomShortcut can't send fn on its own, and its RunShellCommand
// fires once per press with no release event, so holding isn't possible. Wispr
// doesn't report whether it is recording, so a state file alternates presses
// between start and stop. If it falls out of step (dictation ended from the
// keyboard), one extra press re-syncs it.
//
// setup.sh builds this into ~/.config/openlogi/bin/fn-key on macOS.

#include <ApplicationServices/ApplicationServices.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static const CGKeyCode kFn = 63;
static const useconds_t kTapMicros = 40 * 1000;
static const useconds_t kGapMicros = 90 * 1000;

static void post_fn(CGEventSourceRef source, bool down) {
    CGEventRef event = CGEventCreateKeyboardEvent(source, kFn, down);
    CGEventSetType(event, kCGEventFlagsChanged);
    CGEventSetFlags(event, down ? kCGEventFlagMaskSecondaryFn : 0);
    CGEventPost(kCGHIDEventTap, event);
    CFRelease(event);
}

static void tap(CGEventSourceRef source) {
    post_fn(source, true);
    usleep(kTapMicros);
    post_fn(source, false);
}

static int read_recording(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    int recording = 0;
    if (fscanf(f, "%d", &recording) != 1) recording = 0;
    fclose(f);
    return recording;
}

static void write_recording(const char *path, int recording) {
    FILE *f = fopen(path, "w");
    if (!f) return;
    fprintf(f, "%d\n", recording);
    fclose(f);
}

int main(void) {
    const char *tmp = getenv("TMPDIR");
    char state_path[1024];
    snprintf(state_path, sizeof state_path, "%s/openlogi-fn-key.state", tmp ? tmp : "/tmp");

    int recording = read_recording(state_path);
    CGEventSourceRef source = CGEventSourceCreate(kCGEventSourceStateHIDSystemState);
    tap(source);
    if (!recording) {
        usleep(kGapMicros);
        tap(source);
    }
    if (source) CFRelease(source);
    write_recording(state_path, !recording);
    return 0;
}
