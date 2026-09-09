import * as THREE from 'three';
import { VRMSpringBoneColliderShape } from './VRMSpringBoneColliderShape';
export declare class VRMSpringBoneColliderShapeSphere extends VRMSpringBoneColliderShape {
    get type(): 'sphere';
    /**
     * The offset of the sphere from the origin in local space.
     */
    offset: THREE.Vector3;
    /**
     * The radius.
     */
    radius: number;
    /**
     * If true, the collider prevents spring bones from going outside of the sphere instead.
     */
    inside: boolean;
    constructor(params?: {
        radius?: number;
        offset?: THREE.Vector3;
        inside?: boolean;
    });
    calculateCollision(colliderMatrix: THREE.Matrix4, objectPosition: THREE.Vector3, objectRadius: number, target: THREE.Vector3): number;
}
