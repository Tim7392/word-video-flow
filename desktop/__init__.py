"""Desktop editor package.

The window uses text_preview (text placements + a background still), not a video
canvas or PCM clock. Importing the package does not eagerly open the Qt stack;
headless application and packaging callers can import its non-UI modules safely.
"""
