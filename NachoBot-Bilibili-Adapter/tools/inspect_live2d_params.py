import os
import sys
import time
import live2d.v3 as live2d


def main():
    # Initialize PyGame for OpenGL context
    import pygame
    from pygame.locals import DOUBLEBUF, OPENGL

    pygame.init()
    pygame.display.set_mode((800, 600), DOUBLEBUF | OPENGL)

    # Initialize Live2D
    live2d.init()
    live2d.glInit()

    # Path to model
    model_path = r"C:\Users\BigSh0t\Nacho-with-u\NachoBot-Bilibili-Adapter\Resources\hiyori_test\hiyori_pro_zh\runtime\hiyori_pro_t11.model3.json"

    if not os.path.exists(model_path):
        print(f"Model not found: {model_path}")
        return

    print(f"Loading model: {model_path}")

    # Create model
    model = live2d.LAppModel()
    model.LoadModelJson(model_path)
    model.Resize(800, 600)

    # Get Parameters
    param_count = model.GetParameterCount()
    print(f"Parameter Count: {param_count}")

    # Try to get IDs - typically GetParamIds() returns a list of strings
    try:
        # Note: function name might vary slightly in bindings, but log showed 'GetParamIds' exists
        ids = model.GetParamIds()
        print("\n=== Model Parameters ===")
        for i, pid in enumerate(ids):
            value = model.GetParameterValue(pid)
            print(f"{i}: {pid} (Current: {value})")

        print("\n=== Check Specific Params ===")
        targets = ["ParamAngleZ", "ParamBodyAngleX", "ParamEyeLOpen", "ParamMouthOpenY"]
        for t in targets:
            if t in ids:
                print(f"[OK] {t} exists.")
            else:
                print(f"[FAIL] {t} DOES NOT exist!")

    except Exception as e:
        print(f"Error getting params: {e}")

    # Dispose
    live2d.dispose()


if __name__ == "__main__":
    main()
