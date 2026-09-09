import * as THREE from 'three';
import { VRMSpringBoneColliderShape } from './VRMSpringBoneColliderShape';
export declare class VRMSpringBoneColliderShapePlane extends VRMSpringBoneColliderShape {
    get type(): 'plane';
    /**
     * The offset of the plane from the origin in local space.
     */
    offset: THREE.Vector3;
    /**
     * The normal of the plane in local space. Must be normalized.
     */
    normal: THREE.Vector3;
    constructor(params?: {
        offset?: THREE.Vector3;
        normal?: THREE.Vector3;
    });
    calculateCollision(colliderMatrix: THREE.Matrix4, objectPosition: THREE.Vector3, objectRadius: number, target: THREE.Vector3): number;
}
