import numpy as np
import bpy
from bpy.types import Panel, Operator, Object, PropertyGroup, Image, Material
from bpy.props import IntProperty, BoolProperty

uv_baker_data = {}

class OBJECT_OT_prepare_for_uv_unwrap(Operator):
    bl_idname = "object.prepare_for_uv_unwrap"
    bl_label = "Prepare object for UV unwrap"

    def get_face_materials(self, obj: Object):
        if obj is None: raise ValueError("No object selected.")

        mesh = obj.data
        mesh.calc_loop_triangles()
        face_materials = [face.material_index for face in mesh.polygons]

        return face_materials

    def save_obj_state(self, obj: Object):
        uv_baker_data["original_face_materials"] = self.get_face_materials(obj)
        uv_baker_data["original_uv_map"] = obj.data.uv_layers.active
        uv_baker_data["last_obj_name"] = obj.name

    def create_uv_grid_mat(self):
        uv_grid_mat = bpy.data.materials.new("uv-baker_uv-grid")
        uv_grid_mat.use_nodes = True

        shader = uv_grid_mat.node_tree.nodes["Principled BSDF"]
        texture = uv_grid_mat.node_tree.nodes.new('ShaderNodeTexImage')
        bpy.ops.image.new(
            name = "uv-baker_uv-grid",
            generated_type = "COLOR_GRID",
        )
        img = next(img for img in bpy.data.images if img.name == "uv-baker_uv-grid")
        # img = bpy.data.images['uv-baker_uv-grid']
        texture.image = img
        # texture.image = next(img for img in bpy.data.images if img.name == "uv-baker_uv-grid")
        uv_grid_mat.node_tree.links.new(shader.inputs['Base Color'], texture.outputs['Color'])

        return uv_grid_mat

    def get_uv_grid_mat(self):
        # check if there's already a material for a uv grid available in the blender file
        uv_grid_mat = next((material for material in bpy.data.materials if material.name == "uv-baker_uv-grid"), None)
        if uv_grid_mat is not None: return uv_grid_mat
        else: return self.create_uv_grid_mat()

    def prep_uv_unwrap(self, obj: Object):
        uv_grid_mat = self.get_uv_grid_mat()
        obj.data.materials.append(uv_grid_mat)

        # get the index of the material we just added
        idx = len(obj.data.materials) - 1

        # apply new material to all faces
        mesh = obj.data
        mesh.calc_loop_triangles()
        for face in mesh.polygons:
            face.material_index = idx

        # create a new uv map for unwrap
        unwrap_uv = obj.data.uv_layers.new(
            name = "Unwrap",
            do_init = True,
        )

        unwrap_uv.active = True

    def execute(self, context):
        obj = context.active_object
        self.save_obj_state(obj)
        self.prep_uv_unwrap(obj)
        return { "FINISHED" }
    
class OBJECT_OT_reapply_mat_and_bake(Operator):
    bl_idname = "object.reapply_mat_and_bake"
    bl_label = "Reapply original materials and bake them onto prepared UV map"

    def combine_alpha(self, bake: Image, alpha_bake: Image) -> Image:
        rgb = np.array(bake.pixels).reshape(bake.size[0], bake.size[1], 4)
        alpha = np.array(alpha_bake.pixels).reshape(bake.size[0], bake.size[1], 4)

        combined = np.zeros(shape = (bake.size[0], bake.size[1], 4))

        combined[:,:,:3] = rgb[:,:,:3]
        combined[:,:,3] = alpha[:,:,3]

        flat = combined.flatten()

        new_image = bpy.data.images.new(bake.name + "_cutout", bake.size[0], bake.size[1])

        new_image.pixels = list(flat)

        return new_image

    def bake_materials(self, obj: Object):
        mesh = obj.data
        mesh.calc_loop_triangles()

        bake_img = bpy.data.images.new(
            name = obj.name + "_bake",
            width = self.settings.uv_baker_res,
            height = self.settings.uv_baker_res,
            alpha=True,
        )

        alpha_bake_img = None
        if self.settings.bake_alpha:
            alpha_bake_img = bpy.data.images.new(
                name = obj.name + "_bake-alpha",
                width = self.settings.uv_baker_res,
                height = self.settings.uv_baker_res,
                alpha = True
            )

        # remove the uv grid material
        obj.data.materials.pop(
            index=obj.material_slots['uv-baker_uv-grid'].slot_index
        )

        # reapply the materials as they were originally
        for face_idx in range(len(mesh.polygons)):
            mesh.polygons[face_idx].material_index = uv_baker_data['original_face_materials'][face_idx]
    
        for mat in obj.data.materials:
            # link texture to the emission input of its bsdf shader
            shader = mat.node_tree.nodes["Principled BSDF"]
            texture = next((node for node in mat.node_tree.nodes if node.bl_idname == 'ShaderNodeTexImage'), None)
            if texture is None: continue
            mat.node_tree.links.new(shader.inputs['Emission'], texture.outputs['Color'])

            # create a new image texture node to bake to
            bake_node = mat.node_tree.nodes.new('ShaderNodeTexImage')
            bake_node.image = bake_img
            bake_node.select = True
            mat.node_tree.nodes.active = bake_node
        
        # bake
        bpy.ops.object.bake(type = 'EMIT')
        final_bake_img = bake_img

        # for alpha bake, setup nodes for alpha bake
        if alpha_bake_img is not None:
            for mat in obj.data.materials:
                mat.node_tree.links.new(shader.inputs['Alpha'], texture.outputs['Alpha'])
                alpha_bake_node = mat.node_tree.nodes.new('ShaderNodeTexImage')
                alpha_bake_node.image = alpha_bake_img
                alpha_bake_node.select = True
                mat.node_tree.nodes.active = alpha_bake_node
            bpy.ops.object.bake()
            final_bake_img = self.combine_alpha(bake_img, alpha_bake_img)


        # unlink all the other materials
        # obj.data.materials.clear()

        # add new bake material
        bake_mat = self.create_bake_material(obj, final_bake_img)
        obj.data.materials.append(bake_mat)

    def create_bake_material(self, obj: Object, bake_img: Image) -> Material:
        bake_mat = bpy.data.materials.new(f"{obj.name}-bake")
        bake_mat.use_nodes = True

        shader = bake_mat.node_tree.nodes["Principled BSDF"]
        texture = bake_mat.node_tree.nodes.new('ShaderNodeTexImage')
        texture.image = bake_img

        bake_mat.node_tree.links.new(shader.inputs['Base Color'], texture.outputs['Color'])

        return bake_mat

    def execute(self, context):
        obj = context.active_object
        self.settings = context.scene.uv_baker_settings
        if "last_obj_name" not in uv_baker_data: return
        
        obj.select_set(True)

        self.bake_materials(obj)

        return { "FINISHED" }

class VIEW3D_PT_baker_panel(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Tool"
    bl_label = "UV Baker"

    def draw(self, context):
        col = self.layout.column(align=True)
        row = col.row()
        row.prop(bpy.context.scene.uv_baker_settings, "uv_baker_res")
        
        row = col.row()
        row.prop(bpy.context.scene.uv_baker_settings, "bake_alpha")

        row = col.row()
        row.operator("object.prepare_for_uv_unwrap", text="Prepare Unwrap")

        row = col.row()
        row.operator("object.reapply_mat_and_bake", text="Reapply and Bake")

class UVBAKER_settings(PropertyGroup):
    uv_baker_res: IntProperty(
        name = "UV Baker Resolution",
        default = 256,
        min = 1,
        soft_max = 8 * 1024
    ) # type: ignore

    bake_alpha: BoolProperty(
        name = "Bake Alpha Cutout",
        default = False
    ) # type: ignore

def register():
    bpy.utils.register_class(VIEW3D_PT_baker_panel)
    bpy.utils.register_class(OBJECT_OT_prepare_for_uv_unwrap)
    bpy.utils.register_class(OBJECT_OT_reapply_mat_and_bake)
    bpy.utils.register_class(UVBAKER_settings)

    bpy.types.Scene.uv_baker_settings = bpy.props.PointerProperty(type=UVBAKER_settings)


def unregister():
    bpy.utils.unregister_class(VIEW3D_PT_baker_panel)
    bpy.utils.unregister_class(OBJECT_OT_prepare_for_uv_unwrap)
    bpy.utils.unregister_class(OBJECT_OT_reapply_mat_and_bake)
    bpy.utils.unregister_class(UVBAKER_settings)

    del bpy.types.Scene.uv_baker_settings

if __name__ == "__main__":
    register()