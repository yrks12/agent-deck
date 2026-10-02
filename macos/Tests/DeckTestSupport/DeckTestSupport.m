#import <AppKit/AppKit.h>
#import <objc/runtime.h>
#import "DeckTestSupport.h"

// Every window a test creates would otherwise fade on orderOut/close through an
// NSAnimation that runs on a libdispatch worker and never finishes without a
// display. Measured: ~1 leaked worker per window, 64 of them exhaust the pool
// and the Vision tests later deadlock waiting for a thread. Tests never assert
// on window animation, so the whole test process runs with it off. Installed
// at image load, before the first test, so no test file has to remember to.
static NSWindowAnimationBehavior noAnimation(id self, SEL _cmd) { return NSWindowAnimationBehaviorNone; }

__attribute__((constructor)) static void DeckTestSupportInstall(void) {
    Method m = class_getInstanceMethod([NSWindow class], @selector(animationBehavior));
    if (m) method_setImplementation(m, (IMP)noAnimation);
}

void DeckTestSupportAnchor(void) {}
