# Runs in the bootloader's Tcl thread, before Python and during extraction.
.root.canvas create rectangle 36 328 156 356 -fill #1479ff -outline {} -tags sh_pulse
set sh_phase 0
proc sh_animate {} {
    if {![winfo exists .root.canvas]} { return }
    global sh_phase
    set sh_phase [expr {($sh_phase + 1) % 120}]
    set x [expr {36 + 244 * (1 - cos($sh_phase * 0.0523598776))}]
    .root.canvas coords sh_pulse $x 328 [expr {$x + 120}] 356
    after 33 sh_animate
}
sh_animate
