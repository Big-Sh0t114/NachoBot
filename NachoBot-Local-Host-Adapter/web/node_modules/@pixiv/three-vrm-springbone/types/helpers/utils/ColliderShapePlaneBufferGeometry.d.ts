import * as THREE from 'three';
import { VRMSpringBoneColliderShapePlane } from '../../VRMSpringBoneColliderShapePlane';
import { ColliderShapeBufferGeometry } from './ColliderShapeBufferGeometry';
export declare class ColliderShapePlaneBufferGeometry extends THREE.BufferGeometry implements ColliderShapeBufferGeometry {
    worldScale: number;
    private readonly _attrPos;
    private readonly _attrIndex;
    private readonly _shape;
    private readonly _currentOffset;
    private readonly _currentNormal;
    constructor(shape: VRMSpringBoneColliderShapePlane);
    update(): void;
    private _buildPosition;
    private _buildIndex;
}
