#!/usr/bin/python

# Standard dependencies
import sys
import os
import math
import rospy
import numpy as np
import tf
import tf2_ros
from scipy.special import logsumexp # For log weights

from geometry_msgs.msg import Pose, PoseArray, PoseWithCovarianceStamped
from geometry_msgs.msg import Quaternion, TransformStamped
from nav_msgs.msg import Odometry
from tf.transformations import quaternion_from_euler, euler_from_quaternion
from tf.transformations import translation_matrix, translation_from_matrix
from tf.transformations import quaternion_matrix, quaternion_from_matrix

# For sim mbes action client
from sensor_msgs.msg import PointCloud2
import sensor_msgs.point_cloud2 as pc2

# Import Particle() class
from auv_particle import Particle, matrix_from_tf, pcloud2ranges

# Multiprocessing and parallelizing
from multiprocessing import Process, Queue
from resampling import residual_resample, naive_resample, systematic_resample, stratified_resample

# import time # For evaluating mp improvements
# import multiprocessing as mp
# from functools import partial # Might be useful with mp
# from pathos.multiprocessing import ProcessingPool as Pool

class auv_pf(object):

    def __init__(self):
        # Read necessary parameters
        self.pc = rospy.get_param('~particle_count', 10) # Particle Count
        map_frame = rospy.get_param('~map_frame', 'map') # map frame_id
        odom_frame = rospy.get_param('~odom_frame', 'odom')
        meas_model_as = rospy.get_param('~mbes_as', '/mbes_sim_server') # map frame_id
        mbes_pc_top = rospy.get_param("~particle_sim_mbes_topic", '/sim_mbes')
        self.nr_of_processes = rospy.get_param('~number_of_threads', 10) #Nr of threads for parallelizing

        # Initialize tf listener
        tfBuffer = tf2_ros.Buffer()
        tf2_ros.TransformListener(tfBuffer)
        try:
            rospy.loginfo("Waiting for transforms")
            mbes_tf = tfBuffer.lookup_transform('hugin/base_link', 'hugin/mbes_link',
                                                rospy.Time(0), rospy.Duration(10))
            mbes2base_mat = matrix_from_tf(mbes_tf)

            m2o_tf = tfBuffer.lookup_transform('map', 'odom', rospy.Time(0), rospy.Duration(10))
            m2o_mat = matrix_from_tf(m2o_tf)

            rospy.loginfo("Transforms locked - pf node")
        except:
            rospy.loginfo("ERROR: Could not lookup transform from base_link to mbes_link")

        # Multiprocessing
        chunk_size = self.pc/self.nr_of_processes #Nr of particles per process.
        self.out_queue = Queue(maxsize = self.pc)
        self.in_queues = []
        for i in range(self.nr_of_processes):
            self.in_queues.append(Queue(maxsize = chunk_size)) #Queues to make sure all processes are done before publishing.
        self.motion_prediction_params = [] #To account for the different frequencies of motion model updates and meas updates.

        # Read covariance values
        meas_cov = float(rospy.get_param('~measurement_covariance', 0.01))
        cov_string = rospy.get_param('~motion_covariance')
        cov_string = cov_string.replace('[','')
        cov_string = cov_string.replace(']','')
        cov_list = list(cov_string.split(", "))
        motion_cov = list(map(float, cov_list))

        cov_string = rospy.get_param('~init_covariance')
        cov_string = cov_string.replace('[','')
        cov_string = cov_string.replace(']','')
        cov_list = list(cov_string.split(", "))
        init_cov = list(map(float, cov_list))

        self.time = None
        self.old_time = None
        self.pred_odom = None
        self.latest_mbes = PointCloud2()
        self.prev_mbes = PointCloud2()
        self.poses = PoseArray()
        self.poses.header.frame_id = odom_frame
        self.avg_pose = PoseWithCovarianceStamped()
        self.avg_pose.header.frame_id = odom_frame


        #Create all particles
        particles = np.empty(self.pc, dtype=object)
        for i in range(self.pc):
            particles[i] = Particle(i, self.pc, mbes2base_mat, m2o_mat, init_cov=init_cov, meas_cov=meas_cov,
                                 process_cov=motion_cov, map_frame=map_frame, odom_frame=odom_frame,
                                 meas_as=meas_model_as, pc_mbes_top = mbes_pc_top)


        #Start multiprocessing and distribute particles to different processes
        processes = []
        for queue_id, start_id in enumerate(range(0, self.pc, chunk_size)):
            if start_id + chunk_size > self.pc:
                end_id = self.pc
            else:
                end_id = start_id + chunk_size
            p = Process(target=multiprocess_pf, args=(start_id, end_id, particles[start_id:end_id], self.in_queues[queue_id],  self.out_queue))
            processes.append(p)

        for p in processes:
            p.start()
        #-------------------------------------------------------------------------------------------------



        # Initialize particle poses publisher
        pose_array_top = rospy.get_param("~particle_poses_topic", '/particle_poses')
        self.pf_pub = rospy.Publisher(pose_array_top, PoseArray, queue_size=10)

        # Initialize average of poses publisher
        avg_pose_top = rospy.get_param("~average_pose_topic", '/average_pose')
        self.avg_pub = rospy.Publisher(avg_pose_top, PoseWithCovarianceStamped, queue_size=10)

        # Establish subscription to mbes pings message
        mbes_pings_top = rospy.get_param("~mbes_pings_topic", 'mbes_pings')
        rospy.Subscriber(mbes_pings_top, PointCloud2, self.mbes_callback)

        # Establish subscription to odometry message (intentionally last)
        odom_top = rospy.get_param("~odometry_topic", 'odom')
        rospy.Subscriber(odom_top, Odometry, self.odom_callback)
        rospy.loginfo("Particle filter class successfully created")


        # self.update_rviz()
        rospy.spin()

    def mbes_callback(self, msg):
        self.latest_mbes = msg

    def odom_callback(self, odom_msg):
        self.time = odom_msg.header.stamp.to_sec()
        if self.old_time and self.time > self.old_time:
            dt = self.time - self.old_time
            self.motion_prediction_params.append((odom_msg, dt))

            if self.latest_mbes.header.stamp > self.prev_mbes.header.stamp:
                # Measurement update if new one received
                self.prev_mbes = self.latest_mbes

                mbes_ranges = self.update(self.latest_mbes, odom_msg)
                q_input = (mbes_ranges, self.motion_prediction_params)

                for q in self.in_queues:
                    q.put(q_input)

                counter = 0
                weights = np.zeros(0)
                ids = np.zeros(0)
                while counter < self.nr_of_processes:
                    print(counter)
                    output = self.out_queue.get()
                    weights = np.concatenate(weights,output[0])
                    ids = np.concatenate(ids, output[1])
                    counter += 1
                # Particle resampling
                self.resample(weights)

            #self.update_rviz()
        self.old_time = self.time


    def update(self, meas_mbes, odom):
        mbes_meas_ranges = pcloud2ranges(meas_mbes, odom.pose.pose)

        return mbes_meas_ranges

    def resample(self, weights):

        print "-------------"
        # Normalize weights
        weights /= weights.sum()
        #  print "Weights"
        #  print weights

        N_eff = self.pc
        if weights.sum() == 0.:
            rospy.loginfo("All weights zero!")
        else:
            N_eff = 1/np.sum(np.square(weights))

        print "N_eff ", N_eff
        # Resampling?
        if N_eff < self.pc*0.5:
            indices = residual_resample(weights)
            print "Indices"
            print indices
            keep = list(set(indices))
            lost = [i for i in range(self.pc) if i not in keep]
            dupes = indices[:].tolist()
            for i in keep:
                dupes.remove(i)

            self.reassign_poses(lost, dupes)

            # Add noise to particles
            for i in range(self.pc):
                self.particles[i].add_noise([3.,3.,0.,0.,0.,0.0])

        else:
            print N_eff
            rospy.loginfo('Number of effective particles high - not resampling')

    def reassign_poses(self, lost, dupes):
        for i in range(len(lost)):
            # Faster to do separately than using deepcopy()
            self.particles[lost[i]].p_pose.position.x = self.particles[dupes[i]].p_pose.position.x
            self.particles[lost[i]].p_pose.position.y = self.particles[dupes[i]].p_pose.position.y
            self.particles[lost[i]].p_pose.position.z = self.particles[dupes[i]].p_pose.position.z
            self.particles[lost[i]].p_pose.orientation.x = self.particles[dupes[i]].p_pose.orientation.x
            self.particles[lost[i]].p_pose.orientation.y = self.particles[dupes[i]].p_pose.orientation.y
            self.particles[lost[i]].p_pose.orientation.z = self.particles[dupes[i]].p_pose.orientation.z
            self.particles[lost[i]].p_pose.orientation.w = self.particles[dupes[i]].p_pose.orientation.w

    def average_pose(self, pose_list):
        """
        Get average pose of particles and
        publish it as PoseWithCovarianceStamped

        :param pose_list: List of lists containing pose
                        of all particles in form
                        [x, y, z, roll, pitch, yaw]
        :type pose_list: list
            """
        poses_array = np.array(pose_list)
        ave_pose = poses_array.mean(axis = 0)

        self.avg_pose.pose.pose.position.x = ave_pose[0]
        self.avg_pose.pose.pose.position.y = ave_pose[1]
        """
        If z, roll, and pitch can stay as read directly from
        the odometry message there is no need to average them.
        We could just read from any arbitrary particle
        """
        self.avg_pose.pose.pose.position.z = ave_pose[2]
        roll  = ave_pose[3]
        pitch = ave_pose[4]
        """
        Average of yaw angles creates
        issues when heading towards pi because pi and
        negative pi are next to eachother, but average
        out to zero (opposite direction of heading)
        """
        yaws = poses_array[:,5]
        if np.abs(yaws).min() > math.pi/2:
            yaws[yaws < 0] += 2*math.pi
        yaw = yaws.mean()

        self.avg_pose.pose.pose.orientation = Quaternion(*quaternion_from_euler(roll, pitch, yaw))
        self.avg_pose.header.stamp = rospy.Time.now()
        self.avg_pub.publish(self.avg_pose)


    # TODO: publish markers instead of poses
    #       Optimize this function
    def update_rviz(self):
        self.poses.poses = []
        pose_list = []
        for i in range(self.pc):
            self.poses.poses.append(self.particles[i].p_pose)
            pose_vec = self.particles[i].get_pose_vec()
            pose_list.append(pose_vec)
        # Publish particles with time odometry was received
        self.poses.header.stamp = rospy.Time.now()
        self.pf_pub.publish(self.poses)
        self.average_pose(pose_list)

def multiprocess_pf(start_id, end_id, particles, in_queue, out_queue):
    """REMOVE CLASS AND CREATE A SIMPLTE INPUT OUTPUT FUNCTION"""
    nr_of_particles = end_id - start_id
    while True:
        if in_queue.empty():
            rospy.sleep(0.05)
        else:
            #Get data from last time step.
            (mbes_ranges, odom_list) = in_queue.get()
            for odom_elem in odom_list:
                odom_msg = odom_elem[0]
                dt = odom_elem[1]
                predict(odom_msg, dt, nr_of_particles, particles)
            out_queue.put(update(mbes_ranges, particles, start_id, end_id)) #output = (weights, ids)
            print(out_queue.empty)
def predict(odom_t, dt, nr_of_particles, particles):
    for i in range(0, nr_of_particles):
        particles[i].motion_pred(odom_t, dt)

def update(mbes_meas_ranges, particles, start_id, end_id):
    weights = []
    ids = []

    for i, id in enumerate(range(start_id, end_id)):
        particles[i].meas_update(mbes_meas_ranges)
        weights.append(particles[i].w)
        ids.append(id)

    weights_array = np.asarray(weights)
    # Add small non-zero value to avoid hitting zero
    weights_array += 1.e-30

    return (weights_array, ids)


if __name__ == '__main__':

    rospy.init_node('auv_pf')
    try:
        auv_pf()
    except rospy.ROSInterruptException:
        rospy.logerr("Couldn't launch pf")
        pass
