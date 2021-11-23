import time
import weakref

import numpy as np
import wgpu.backends.rs

from .. import Renderer
from ...linalg import Matrix4, Vector3
from ...objects import WorldObject
from ...cameras import Camera
from ...resources import Buffer, Texture, TextureView
from ...utils import array_from_shadertype

from . import _blender as blender_module
from ._pipelinebuilder import ensure_pipeline
from ._update import update_buffer, update_texture, update_texture_view


# Definition uniform struct with standard info related to transforms,
# provided to each shader as uniform at slot 0.
# todo: a combined transform would be nice too, for performance
# todo: same for ndc_to_world transform (combined inv transforms)
stdinfo_uniform_type = dict(
    cam_transform="4x4xf4",
    cam_transform_inv="4x4xf4",
    projection_transform="4x4xf4",
    projection_transform_inv="4x4xf4",
    physical_size="2xf4",
    logical_size="2xf4",
    flipped_winding="i4",  # A bool, really
)


def get_size_from_render_target(target):
    """Get physical and logical size from a render target."""
    if isinstance(target, wgpu.gui.WgpuCanvasBase):
        physical_size = target.get_physical_size()
        logical_size = target.get_logical_size()
    elif isinstance(target, Texture):
        physical_size = target.size[:2]
        logical_size = physical_size
    elif isinstance(target, TextureView):
        physical_size = target.texture.size[:2]
        logical_size = physical_size
    else:
        raise TypeError(f"Unexpected render target {target.__class__.__name__}")
    return physical_size, logical_size


class SharedData:
    """An object to store global data to share between multiple wgpu renderers.

    Since renderers don't render simultaneously, they can share certain
    resources. This safes memory, but more importantly, resources that
    get used in wobject pipelines should be shared to avoid having to
    constantly recompose the pipelines of wobjects that are rendered by
    multiple renderers.
    """

    def __init__(self, canvas):

        # Create adapter and device objects - there should be just one per canvas.
        # Having a global device provides the benefit that we can draw any object
        # anywhere.
        # We do pass the canvas to request_adapter(), so we get an adapter that is
        # at least compatible with the first canvas that a renderer is create for.
        self.adapter = wgpu.request_adapter(
            canvas=canvas, power_preference="high-performance"
        )
        self.device = self.adapter.request_device(
            required_features=[], required_limits={}
        )

        # Create a uniform buffer for std info
        self.stdinfo_buffer = Buffer(array_from_shadertype(stdinfo_uniform_type))
        self.stdinfo_buffer._wgpu_usage |= wgpu.BufferUsage.UNIFORM

        # A cache for shader objects
        self.shader_cache = {}


class WgpuRenderer(Renderer):
    """Object used to render scenes using wgpu.

    The purpose of a renderer is to render (i.e. draw) a scene to a
    canvas or texture. It also provides picking, defines the
    anti-aliasing parameters, and any post processing effects.

    A renderer is directly associated with its target and can only render
    to that target. Different renderers can render to the same target though.

    It provides a ``.render()`` method that can be called one or more
    times to render scene. This creates a visual representation that
    is stored internally, and is finally rendered into its render target
    (the canvas or texture).
                                  __________
                                 | renderer |
        [scenes] -- render() --> |  state   | -- flush() --> [target]
                                 |__________|

    The internal visual representation includes things like a depth
    buffer and is typically at a higher resolution to reduce aliasing
    effects. Further, the representation may in the future accomodate
    for proper blending of semitransparent objects.

    The flush-step renders the internal representation into the target
    texture or canvas, applying anti-aliasing. In the future this is
    also where fog is applied, as well as any custom post-processing
    effects.

    Parameters:
        target (WgpuCanvas or Texture): The target to render to, and what
            determines the size of the render buffer.
        pixel_ratio (float, optional): How large the physical size of the render
            buffer is in relation to the target's physical size, for antialiasing.
            See the corresponding property for details.
        show_fps (bool): Whether to display the frames per second. Beware that
            depending on the GUI toolkit, the canvas may impose a frame rate limit.
    """

    _shared = None

    _wobject_pipelines_collection = weakref.WeakValueDictionary()

    def __init__(self, target, *, pixel_ratio=None, show_fps=False):

        # Check and normalize inputs
        if not isinstance(target, (Texture, TextureView, wgpu.gui.WgpuCanvasBase)):
            raise TypeError(
                f"Render target must be a canvas or texture (view), not a {target.__class__.__name__}"
            )
        self._target = target

        # Process other inputs
        self.pixel_ratio = pixel_ratio
        self._show_fps = bool(show_fps)

        # Make sure we have a shared object (the first renderer create it)
        canvas = target if isinstance(target, wgpu.gui.WgpuCanvasBase) else None
        if WgpuRenderer._shared is None:
            WgpuRenderer._shared = SharedData(canvas)

        # Init counter to auto-clear
        self._renders_since_last_flush = 0

        # Get target format
        if isinstance(target, wgpu.gui.WgpuCanvasBase):
            self._canvas_context = self._target.get_context()
            self._target_tex_format = self._canvas_context.get_preferred_format(
                self._shared.adapter
            )
            # Also configure the canvas
            self._canvas_context.configure(
                device=self._shared.device,
                format=self._target_tex_format,
                usage=wgpu.TextureUsage.RENDER_ATTACHMENT,
            )
        else:
            self._target_tex_format = self._target.format
            # Also enable the texture for render and display usage
            self._target._wgpu_usage |= wgpu.TextureUsage.RENDER_ATTACHMENT
            self._target._wgpu_usage |= wgpu.TextureUsage.TEXTURE_BINDING

        # Prepare render targets.
        self.blend_mode = "default"

        # Prepare object that performs the final render step into a texture
        self._flusher = blender_module.RenderFlusher(self._shared.device)

        # Initialize a small buffer to read pixel info into
        # Make it 256 bytes just in case (for bytes_per_row)
        self._pixel_info_buffer = self._shared.device.create_buffer(
            size=256,
            usage=wgpu.BufferUsage.COPY_DST | wgpu.BufferUsage.MAP_READ,
        )

        # Keep track of object ids
        self._pick_map = weakref.WeakValueDictionary()

    @property
    def device(self):
        """A reference to the used wgpu device."""
        return self._shared.device

    @property
    def target(self):
        """The render target. Can be a canvas, texture or texture view."""
        return self._target

    @property
    def pixel_ratio(self):
        """The ratio between the number of internal pixels versus the logical pixels on the canvas.

        This can be used to configure the size of the render texture
        relative to the canvas' logical size. By default (value is None) the
        used pixel ratio follows the screens pixel ratio on high-res
        displays, and is 2 otherwise.

        If the used pixel ratio causes the render texture to be larger
        than the physical size of the canvas, SSAA is applied, resulting
        in a smoother final image with less jagged edges. Alternatively,
        this value can be set to e.g. 0.5 to lower* the resolution (e.g.
        for performance during interaction).
        """
        return self._pixel_ratio

    @pixel_ratio.setter
    def pixel_ratio(self, value):
        if value is None:
            self._pixel_ratio = None
        elif isinstance(value, (int, float)):
            self._pixel_ratio = None if value <= 0 else float(value)
        else:
            raise TypeError(
                f"Rendered.pixel_ratio expected None or number, not {value}"
            )

    @property
    def blend_mode(self):
        """The method for handling transparency:
        * "default" or None: Select the default: currently this is "simple2".
        * "opaque": single-pass approach that consider every fragment opaque.
        * "simple1": single-pass approach that blends fragments (using OVER compositing).
          Can only produce correct results if fragments are drawn from back to front.
        * "simple2": two-pass approach that first processes all opaque fragments and
          then blends transparent fragments (using the OVER operator) with depth-write disabled.
          Yields visually ok results, but is not order independent.
        * "weighted": todo McGuire 2013
        * "weighted_z": todo McGuire 2016
        * "multilayer2": todo 2-layer MLAB + weighted for the rest?
        """
        return self._blend_mode

    @blend_mode.setter
    def blend_mode(self, value):
        # Massage and check the input
        if value is None:
            value = "default"
        value = value.lower()
        if value == "default":
            value = "simple2"
        # Map string input to a class
        m = {
            "opaque": blender_module.OpaqueFragmentBlender,
            "simple1": blender_module.Simple1FragmentBlender,
            "simple2": blender_module.Simple2FragmentBlender,
        }
        if value not in m:
            raise ValueError(
                f"Unknown blend_mode '{value}', use any of {set(m.keys())}"
            )
        # Set blender object
        self._blend_mode = value
        self._blender = m[value]()
        # If the blend mode has changed, we may need a new _wobject_pipelines
        self._set_wobject_pipelines()
        # If our target is a canvas, request a new draw
        if isinstance(self._target, wgpu.gui.WgpuCanvasBase):
            self._target.request_draw()

    def _set_wobject_pipelines(self):
        # Each WorldObject has associated with it a wobject_pipeline:
        # a dict that contains the wgpu pipeline objects. This
        # wobject_pipeline is also associated with the blend_mode,
        # because the blend mode affects the pipelines.
        #
        # Each renderer has ._wobject_pipelines, a dict that maps
        # wobject -> wobject_pipeline. This dict is a WeakKeyDictionary -
        # when the wobject is destroyed, the associated pipeline is
        # collected as well.
        #
        # Renderers with the same blend mode can safely share these
        # wobject_pipeline dicts. Therefore, we make use of a global
        # collection. Since this global collection is a
        # WeakValueDictionary, if all renderes stop using a certain
        # blend mode, the associated pipelines are removed as well.
        #
        # In a diagram:
        #
        # _wobject_pipelines_collection -> _wobject_pipelines -> wobject_pipeline
        #        global                         renderer              wobject
        #   WeakValueDictionary              WeakKeyDictionary         dict

        # Below we set this renderer's _wobject_pipelines. Note that if the
        # blending has changed, we automatically invalidate all "our" pipelines.
        self._wobject_pipelines = WgpuRenderer._wobject_pipelines_collection.setdefault(
            self.blend_mode, weakref.WeakKeyDictionary()
        )

    def render(
        self,
        scene: WorldObject,
        camera: Camera,
        *,
        viewport=None,
        clear_color=None,
        clear_depth=None,
        flush=True,
    ):
        """Render a scene with the specified camera as the viewpoint.

        Parameters:
            scene (WorldObject): The scene to render, a WorldObject that
                optionally has child objects.
            camera (Camera): The camera object to use, which defines the
                viewpoint and view transform.
            viewport (tuple, optional): The rectangular region to draw into,
                expressed in logical pixels.
            clear_color (bool, optional): Whether to clear the color buffer
                before rendering. By default this is True on the first
                call to ``render()`` after a flush, and False otherwise.
            clear_depth (bool, optional): Whether to clear the depth buffer
                before rendering. By default this is True on the first
                call to ``render()`` after a flush, and False otherwise.
            flush (bool, optional): Whether to flush the rendered result into
                the target (texture or canvas). Default True.
        """
        device = self.device

        now = time.perf_counter()  # noqa
        if self._show_fps:
            if not hasattr(self, "_fps"):
                self._fps = now, now, 1
            elif now > self._fps[0] + 1:
                print(f"FPS: {self._fps[2]/(now - self._fps[0]):0.1f}")
                self._fps = now, now, 1
            else:
                self._fps = self._fps[0], now, self._fps[2] + 1

        # Define whether to clear color and/or depth
        if clear_color is None:
            clear_color = self._renders_since_last_flush == 0
        clear_color = bool(clear_color)
        if clear_depth is None:
            clear_depth = self._renders_since_last_flush == 0
        clear_depth = bool(clear_depth)
        self._renders_since_last_flush += 1

        # todo: also note that the fragment shader is (should be) optional
        #      (e.g. depth only passes like shadow mapping or z prepass)

        # Get logical size (as two floats). This size is constant throughout
        # all post-processing render passes.
        target_size, logical_size = get_size_from_render_target(self._target)
        if not all(x > 0 for x in logical_size):
            return

        # Determine the physical size of the render texture
        target_pixel_ratio = target_size[0] / logical_size[0]
        if self._pixel_ratio:
            pixel_ratio = self._pixel_ratio
        else:
            pixel_ratio = target_pixel_ratio
            if pixel_ratio <= 1:
                pixel_ratio = 2.0  # use 2 on non-hidpi displays

        # Determine the physical size of the first and last render pass
        framebuffer_size = tuple(max(1, int(pixel_ratio * x)) for x in logical_size)

        # Update the render targets
        self._blender.ensure_target_size(device, framebuffer_size)

        # Get viewport in physical pixels
        if not viewport:
            scene_logical_size = logical_size
            scene_physical_size = framebuffer_size
            physical_viewport = 0, 0, framebuffer_size[0], framebuffer_size[1], 0, 1
        elif len(viewport) == 4:
            scene_logical_size = viewport[2], viewport[3]
            physical_viewport = [int(i * pixel_ratio + 0.4999) for i in viewport]
            physical_viewport = tuple(physical_viewport) + (0, 1)
            scene_physical_size = physical_viewport[2], physical_viewport[3]
        else:
            raise ValueError("The viewport must be None or 4 elements (x, y, w, h).")

        # Ensure that matrices are up-to-date
        scene.update_matrix_world()
        camera.set_view_size(*scene_logical_size)
        camera.update_matrix_world()  # camera may not be a member of the scene
        camera.update_projection_matrix()

        # Get the list of objects to render (visible and having a material)
        q = self.get_render_list(scene, camera)
        for wobject in q:
            self._pick_map[wobject.id] = wobject

        # Update stdinfo uniform buffer object that we'll use during this render call
        self._update_stdinfo_buffer(camera, scene_physical_size, scene_logical_size)

        # Ensure each wobject has pipeline info, and filter objects that we cannot render
        wobject_tuples = []
        for wobject in q:
            wobject_pipeline = ensure_pipeline(self, wobject)
            if wobject_pipeline:
                wobject_tuples.append((wobject, wobject_pipeline))

        # Render the scene graph (to the first texture)
        command_encoder = device.create_command_encoder()
        self._render_recording(
            command_encoder, wobject_tuples, physical_viewport, clear_color, clear_depth
        )
        command_buffers = [command_encoder.finish()]
        device.queue.submit(command_buffers)

        # Flush to target
        if flush:
            self.flush()

    def flush(self):
        """Render the result into the target texture view. This method is
        called automatically unless you use ``.render(..., flush=False)``.
        """

        # Note: we could, in theory, allow specifying a custom target here.

        if isinstance(self._target, wgpu.gui.WgpuCanvasBase):
            raw_texture_view = self._canvas_context.get_current_texture()
        else:
            if isinstance(self._target, Texture):
                texture_view = self._target.get_view()
            elif isinstance(self._target, TextureView):
                texture_view = self._target
            update_texture(self._shared.device, texture_view.texture)
            update_texture_view(self._shared.device, texture_view)
            raw_texture_view = texture_view._wgpu_texture_view[1]

        self._flusher.render(
            self._blender.color_view,
            None,
            raw_texture_view,
            self._target_tex_format,
        )

        # Reset counter (so we can auto-clear the first next draw)
        self._renders_since_last_flush = 0

    def _render_recording(
        self,
        command_encoder,
        wobject_tuples,
        physical_viewport,
        clear_color,
        clear_depth,
    ):

        # You might think that this is slow for large number of world
        # object. But it is actually pretty good. It does iterate over
        # all world objects, and over stuff in each object. But that's
        # it, really.
        # todo: we may be able to speed this up with render bundles though

        # ----- compute pipelines

        compute_pass = command_encoder.begin_compute_pass()

        for wobject, wobject_pipeline in wobject_tuples:
            for pinfo in wobject_pipeline["compute_pipelines"]:
                compute_pass.set_pipeline(pinfo["pipeline"])
                for bind_group_id, bind_group in enumerate(pinfo["bind_groups"]):
                    compute_pass.set_bind_group(
                        bind_group_id, bind_group, [], 0, 999999
                    )
                compute_pass.dispatch(*pinfo["index_args"])

        compute_pass.end_pass()

        # ----- render pipelines

        for render_pass_iter in [1, 2]:

            if render_pass_iter == 1:
                # Render pass 1 renders opaque fragments and picking info
                color_attachments = self._blender.get_color_attachments1(clear_color)
                depth_load_value = 1.0 if clear_depth else wgpu.LoadOp.load
                depth_store_op = wgpu.StoreOp.store
            else:
                # Render pass 2 renders transparent fragments, as defined by the blender
                color_attachments = self._blender.get_color_attachments2(clear_color)
                depth_load_value = wgpu.LoadOp.load
                # depth_write_enabled is already False for all objects, but we
                # also disable it on the pipeline for good measure.
                depth_store_op = wgpu.StoreOp.discard

            if not color_attachments:
                continue

            render_pass = command_encoder.begin_render_pass(
                color_attachments=color_attachments,
                depth_stencil_attachment={
                    "view": self._blender.depth_view,
                    "depth_load_value": depth_load_value,
                    "depth_store_op": depth_store_op,
                    "stencil_load_value": wgpu.LoadOp.load,
                    "stencil_store_op": wgpu.StoreOp.store,
                },
                occlusion_query_set=None,
            )
            render_pass.set_viewport(*physical_viewport)

            for wobject, wobject_pipeline in wobject_tuples:
                for pinfo in wobject_pipeline["render_pipelines"]:
                    render_pass.set_pipeline(pinfo[f"pipeline{render_pass_iter}"])
                    for slot, vbuffer in pinfo["vertex_buffers"].items():
                        render_pass.set_vertex_buffer(
                            slot,
                            vbuffer._wgpu_buffer[1],
                            vbuffer.vertex_byte_range[0],
                            vbuffer.vertex_byte_range[1],
                        )
                    for bind_group_id, bind_group in enumerate(pinfo["bind_groups"]):
                        render_pass.set_bind_group(bind_group_id, bind_group, [], 0, 99)
                    # Draw with or without index buffer
                    if pinfo["index_buffer"] is not None:
                        ibuffer = pinfo["index_buffer"]
                        render_pass.set_index_buffer(ibuffer, 0, ibuffer.size)
                        render_pass.draw_indexed(*pinfo["index_args"])
                    else:
                        render_pass.draw(*pinfo["index_args"])

            render_pass.end_pass()

    def _update_stdinfo_buffer(self, camera, physical_size, logical_size):
        # Update the stdinfo buffer's data
        stdinfo_data = self._shared.stdinfo_buffer.data
        stdinfo_data["cam_transform"].flat = camera.matrix_world_inverse.elements
        stdinfo_data["cam_transform_inv"].flat = camera.matrix_world.elements
        stdinfo_data["projection_transform"].flat = camera.projection_matrix.elements
        stdinfo_data[
            "projection_transform_inv"
        ].flat = camera.projection_matrix_inverse.elements
        # stdinfo_data["ndc_to_world"].flat = np.linalg.inv(stdinfo_data["cam_transform"] @ stdinfo_data["projection_transform"])
        stdinfo_data["physical_size"] = physical_size
        stdinfo_data["logical_size"] = logical_size
        stdinfo_data["flipped_winding"] = camera.flips_winding
        # Upload to GPU
        self._shared.stdinfo_buffer.update_range(0, 1)
        update_buffer(self._shared.device, self._shared.stdinfo_buffer)

    def get_render_list(self, scene: WorldObject, camera: Camera):
        """Given a scene object, get a flat list of objects to render."""

        # Collect items
        def visit(wobject):
            nonlocal q
            if wobject.material is not None:
                q.append(wobject)

        q = []
        scene.traverse(visit, True)

        # Next, sort them from back-to-front
        def sort_func(wobject: WorldObject):
            z = (
                Vector3()
                .set_from_matrix_position(wobject.matrix_world)
                .apply_matrix4(proj_screen_matrix)
                .z
            )
            return wobject.render_order, z

        proj_screen_matrix = Matrix4().multiply_matrices(
            camera.projection_matrix, camera.matrix_world_inverse
        )
        # todo: either revive or remove (leaning for the latter)
        # q.sort(key=sort_func)
        return q

    # Picking

    def get_pick_info(self, pos):
        """Get information about the given window location. The given
        pos is a 2D point in logical pixels (with the origin at the
        top-left). Returns a dict with fields:

        * "ndc": The position in normalized device coordinates, the 3d element
            being the depth (0..1). Can be translated to the position
            in world coordinates using the camera transforms.
        * "rgba": The value in the color buffer. All zero's when rendering
          directly to the screen (bypassing post-processing).
        * "world_object": the object at that location (provided that
          the object supports picking).
        * Additional pick info may be available, depending on the type of
          object and its material. See the world-object classes for details.
        """

        # Make pos 0..1, so we can scale it to the render texture
        _, logical_size = get_size_from_render_target(self._target)
        float_pos = pos[0] / logical_size[0], pos[1] / logical_size[1]

        can_sample_color = self._blender.color_tex is not None

        # Sample
        encoder = self.device.create_command_encoder()
        self._copy_pixel(encoder, self._blender.depth_tex, float_pos, 0)
        if can_sample_color:
            self._copy_pixel(encoder, self._blender.color_tex, float_pos, 8)
        self._copy_pixel(encoder, self._blender.pick_tex, float_pos, 16)
        queue = self.device.queue
        queue.submit([encoder.finish()])

        # Collect data from the buffer
        data = self._pixel_info_buffer.map_read()
        depth = data[0:4].cast("f")[0]
        color = tuple(data[8:12].cast("B"))
        pick_value = tuple(data[16:32].cast("i"))
        wobject = self._pick_map.get(pick_value[0], None)
        # Note: the position in world coordinates is not included because
        # it depends on the camera, but we don't "own" the camera.

        info = {
            "ndc": (2 * float_pos[0] - 1, 2 * float_pos[1] - 1, depth),
            "rgba": color if can_sample_color else (0, 0, 0, 0),
            "world_object": wobject,
        }

        if wobject and wobject.material is not None:
            pick_info = wobject._wgpu_get_pick_info(pick_value)
            info.update(pick_info)
        return info

    def _copy_pixel(self, encoder, render_texture, float_pos, buf_offset):

        # Map position to the texture index
        w, h, d = render_texture.size
        x = max(0, min(w - 1, int(float_pos[0] * w)))
        y = max(0, min(h - 1, int(float_pos[1] * h)))

        # Note: bytes_per_row must be a multiple of 256.
        encoder.copy_texture_to_buffer(
            {
                "texture": render_texture,
                "mip_level": 0,
                "origin": (x, y, 0),
            },
            {
                "buffer": self._pixel_info_buffer,
                "offset": buf_offset,
                "bytes_per_row": 256,  # render_texture.bytes_per_pixel,
                "rows_per_image": 1,
            },
            copy_size=(1, 1, 1),
        )

    def snapshot(self):
        """Create a snapshot of the currently rendered image."""

        # Prepare
        device = self._shared.device
        texture = self._blender.color_tex
        size = texture.size
        bytes_per_pixel = 4

        # Note, with queue.read_texture the bytes_per_row limitation does not apply.
        data = device.queue.read_texture(
            {
                "texture": texture,
                "mip_level": 0,
                "origin": (0, 0, 0),
            },
            {
                "offset": 0,
                "bytes_per_row": bytes_per_pixel * size[0],
                "rows_per_image": size[1],
            },
            size,
        )

        return np.frombuffer(data, np.uint8).reshape(size[1], size[0], 4)
