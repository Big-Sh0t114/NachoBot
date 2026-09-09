import * as THREE from 'three';
import { VRMSpringBoneColliderShape } from './VRMSpringBoneColliderShape';
export declare class VRMSpringBoneColliderShapeCapsule extends VRMSpringBoneColliderShape {
    get type(): 'capsule';
    /**
     * The offset of the capsule head from the origin in local space.
     */
    offset: THREE.Vector3;
    /**
     * The offset of the capsule tail from the origin in local space.
     */
    tail: THREE.Vector3;
    /**
     * The radius of the capsule.
     */
    radius: number;
    /**
     * If true, the collider prevents spring bones from going outside of the capsule instead.
     */
    inside: boolean;
    constructor(params?: {
        radius?: number;
        offset?: THREE.Vector3;
        tail?: THREE.Vector3;
        inside?: boolean;
    });
    calculateCollision(colliderMatrix: THREE.Matrix4, objectPosition: THREE.Vector3, objectRadius: number, target: THREE.Vector3): number;
}
