"""The Notifier — turns a ``PagePayload`` into a page a human can act on (doc 04 §8).

``render_page`` is a pure function producing the 15-second-approval card (doc 00 §6):
suspected root cause, calibrated confidence, the drafted fix, the evidence chain,
and competing hypotheses. ``Notifier`` is the delivery seam; ``ConsoleNotifier``
prints the card (the offline default) and the production Slack/SMS/voice channels
implement the same Protocol.
"""

from sentinel.notify.page import CapturingNotifier, ConsoleNotifier, Notifier, render_page

__all__ = ["CapturingNotifier", "ConsoleNotifier", "Notifier", "render_page"]
