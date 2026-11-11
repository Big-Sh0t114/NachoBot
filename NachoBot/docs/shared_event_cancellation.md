# Shared event cancellation

The public `EventsManager.handle_nacho_events` boundary uses the shared task cancellation helper. Its entry checkpoint catches cancellation that is already pending, including calls with no subscribers. It also checks after the handler snapshot, yields fairly before each handler's fresh admission gates, and checks again after synchronous plugin configuration is set.

The private interceptor dispatcher enters the same helper scope and performs conditional checks without adding a healthy-path scheduling yield. Direct private calls create their own scope; same-task calls nested under a tool boundary borrow that boundary's cancellation baseline. A child task is a different owner and concurrent event requests keep independent baselines.

If a caller cancels while an interceptor is running, the interceptor task may receive that cancellation. A callback can swallow it, but the event caller checks its own cancellation count after the child finishes and raises cancellation before unpacking or publishing the result. Task tracking is cleaned in the existing `finally` block. No later interceptor or observer is dispatched. An interceptor that ignores cancellation remains awaited until it finishes, so cancellation is cooperative and does not provide a hard interrupt.

Interceptor output is detached before history is committed. Cancellation during input or output copying, log formatting, or custom history truth evaluation prevents the current result and its history from being published. An invalid or uncopyable replacement is discarded while the healthy-path continuation flag and custom history result are retained. Earlier handler side effects and already committed history are not rolled back.

Nonblocking observers already created before cancellation remain in their existing handler task buckets and follow the normal unload and cleanup lifecycle. Event cancellation does not cancel those observer tasks or change the bounded handler-unload policy.
