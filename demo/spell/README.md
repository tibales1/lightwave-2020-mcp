# Spell orb demo

Built entirely through the MCP (2026-10-10):

1. **Modeler** — `MAKEBALL <0.5 0.5 0.5> 64 32 <0 0 0>` (1 m ball, 1986 points / 2048 polygons),
   surface `SpellOrb`; a low-poly shard, a pillar and a ground plane.
2. **Layout** (`layout_run_python`) — load objects, key an arcing flight with pulse and spin, vanish
   on impact at frame 49, 24 shards flying out on randomised paths, an orb light and a blast light
   with lens flares, camera move; violet/orange luminous surfaces.
3. Light/flare flash envelopes added to the `.lws`, then 91 frames rendered at 960×540 in Layout
   (~2 s/frame) → `Renders/spell_orb.mp4`.

To open: copy this folder to `C:\lightwave-mcp\spell` and load `Scenes/spell_orb.lws` in Layout.
Surface colours are applied by the MCP at build time and are not stored in the objects (LWO3).
