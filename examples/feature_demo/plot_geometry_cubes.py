"""
Geometry Cubes
==============

Example showing multiple rotating cubes. This also tests the depth buffer.
"""

# sphinx_gallery_pygfx_render = True
# sphinx_gallery_pygfx_target_name = "disp"

import imageio.v3 as iio
import pygfx as gfx


group = gfx.Group()

im = iio.imread("imageio:chelsea.png")
tex = gfx.Texture(im, dim=2).get_view(filter="linear")

material = gfx.MeshBasicMaterial(map=tex)
geometry = gfx.box_geometry(100, 100, 100)
cubes = [gfx.Mesh(geometry, material) for i in range(8)]
for i, cube in enumerate(cubes):
    cube.position.set(350 - i * 100, 0, 0)
    group.add(cube)


def animate():
    for i, cube in enumerate(cubes):
        rot = gfx.linalg.Quaternion().set_from_euler(
            gfx.linalg.Euler(0.01 * i, 0.02 * i)
        )
        cube.rotation.multiply(rot)


if __name__ == "__main__":
    disp = gfx.Display()
    disp.before_render = animate
    disp.show(group)
