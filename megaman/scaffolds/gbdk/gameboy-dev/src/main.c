// Starter ROM — proves the toolchain and gives a working game loop to build on.
// Prints a message and moves a sprite with the d-pad.

#include <gb/gb.h>
#include <stdio.h>

// One 8x8 tile: a solid block with a notch, so movement is visible.
const unsigned char cursor_tile[] = {
    0xFF, 0xFF, 0x81, 0xFF, 0x81, 0xFF, 0x81, 0xFF,
    0x81, 0xFF, 0x81, 0xFF, 0x81, 0xFF, 0xFF, 0xFF,
};

void main(void) {
    uint8_t x = 80, y = 72;
    uint8_t keys;

    printf(" GBDK-2020 ready\n\n d-pad moves the\n sprite");

    set_sprite_data(0, 1, cursor_tile);
    set_sprite_tile(0, 0);
    SHOW_SPRITES;

    while (1) {
        keys = joypad();
        if (keys & J_LEFT  && x > 8)   x--;
        if (keys & J_RIGHT && x < 160) x++;
        if (keys & J_UP    && y > 16)  y--;
        if (keys & J_DOWN  && y < 152) y++;
        move_sprite(0, x, y);
        vsync();  // one update per frame — all game timing hangs off this
    }
}
