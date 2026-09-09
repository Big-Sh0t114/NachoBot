import * as THREE from 'three';
/**
 * Traverses the given object and combines the skeletons of skinned meshes.
 *
 * Each frame the bone matrices are computed for every skeleton. Combining skeletons
 * reduces the number of calculations needed, improving performance.
 *
 * @param root Root object that will be traversed
 */
export declare function combineSkeletons(root: THREE.Object3D): void;
