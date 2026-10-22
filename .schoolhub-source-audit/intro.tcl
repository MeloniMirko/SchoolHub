# Actual 30 FPS Tk canvas animation displayed while PyInstaller extracts.
# Motion is indeterminate: it never pretends to represent download progress.
if {[winfo exists .root.canvas]} {
    .root.canvas create arc 228 13 452 237 -start 5 -extent 74 \
        -style arc -outline #38bdf8 -width 3 -tags sh_orbit_arc
    .root.canvas create arc 240 25 440 225 -start 195 -extent 88 \
        -style arc -outline #205ca2 -width 2 -tags sh_orbit_arc2
    .root.canvas create oval 0 0 1 1 -fill #7dd3fc -outline {} -tags sh_dot0
    .root.canvas create oval 0 0 1 1 -fill #38bdf8 -outline {} -tags sh_dot1
    .root.canvas create oval 0 0 1 1 -fill #1d83f7 -outline {} -tags sh_dot2
    .root.canvas create rectangle 38 330 154 354 -fill #268bff \
        -outline {} -tags sh_pulse
    set sh_phase 0
    proc sh_animate {} {
        if {![winfo exists .root.canvas]} { return }
        global sh_phase
        set sh_phase [expr {($sh_phase + 3) % 360}]
        set pi 3.141592653589793
        .root.canvas itemconfigure sh_orbit_arc -start [expr {$sh_phase + 8}]
        .root.canvas itemconfigure sh_orbit_arc2 -start [expr {192 - $sh_phase * 0.75}]
        for {set i 0} {$i < 3} {incr i} {
            set theta [expr {($sh_phase + $i * 120) * $pi / 180.0}]
            set px [expr {340 + 108 * cos($theta)}]
            set py [expr {125 + 108 * sin($theta)}]
            set radius [expr {5.5 - $i * 0.8}]
            .root.canvas coords sh_dot$i [expr {$px - $radius}] [expr {$py - $radius}] \
                [expr {$px + $radius}] [expr {$py + $radius}]
        }
        # Animated luminous sweep, not a misleading 0-100% completion meter.
        set p [expr {(1.0 - cos($sh_phase * $pi / 180.0)) / 2.0}]
        set left [expr {38 + 489 * $p}]
        .root.canvas coords sh_pulse $left 330 [expr {$left + 116}] 354
        after 33 sh_animate
    }
    sh_animate
}
