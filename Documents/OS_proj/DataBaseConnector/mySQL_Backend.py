import mysql.connector
from mysql.connector import Error
from datetime import datetime
from typing import Optional


class Database:
    """
    Database interface for the OS Process Monitor.

    Every SELECT function returns:
        list[dict]

    Every insertion function returns:
        inserted ID / True / False
    """

    def __init__(
        self,
        host: str = "localhost",
        user: str = "root",
        password: str = "OS_monitorDB_root_xchdbVw_@3df",
        database: str = "os_internship_process_monitor_proj"
    ):
        self.connection = None

        try:
            self.connection = mysql.connector.connect(
                host=host,
                user=user,
                password=password,
                database=database
            )

            if self.connection.is_connected():
                print("Connected to MySQL database.")

        except Error as e:
            print(f"MySQL connection failed: {e}")
            raise


    # ============================================================
    # GENERAL DATABASE FUNCTIONS
    # ============================================================

    def close(self):
        """Close the database connection."""

        if self.connection and self.connection.is_connected():
            self.connection.close()


    def _execute_select(self, query, params=None):
        """
        Execute SELECT query.

        Returns:
            list[dict]
        """

        cursor = self.connection.cursor(dictionary=True)

        try:
            cursor.execute(query, params or ())
            return cursor.fetchall()

        finally:
            cursor.close()


    def _execute_insert(self, query, params=None):
        """
        Execute INSERT query.

        Returns:
            last inserted ID
        """

        cursor = self.connection.cursor()

        try:
            cursor.execute(query, params or ())
            self.connection.commit()

            return cursor.lastrowid

        except Error:
            self.connection.rollback()
            raise

        finally:
            cursor.close()


    def _execute_update(self, query, params=None):
        """
        Execute UPDATE/DELETE query.
        """

        cursor = self.connection.cursor()

        try:
            cursor.execute(query, params or ())
            self.connection.commit()

            return cursor.rowcount

        except Error:
            self.connection.rollback()
            raise

        finally:
            cursor.close()


    # ============================================================
    # PROCESS QUERIES
    # ============================================================

    def get_all_processes(self):
        """
        Get every process.

        Returns:
            list[dict]
        """

        query = """
            SELECT
                pid,
                burst_time,
                waiting_time,
                completion_time,
                turnaround_time,
                status
            FROM ProcessTable
            ORDER BY pid;
        """

        return self._execute_select(query)


    def get_process(self, pid: int):
        """
        Get one process.
        """

        query = """
            SELECT
                pid,
                burst_time,
                waiting_time,
                completion_time,
                turnaround_time,
                status
            FROM ProcessTable
            WHERE pid = %s;
        """

        result = self._execute_select(query, (pid,))

        return result[0] if result else None


    def get_processes_by_status(self, status: str):
        """
        Get processes with a specific status.
        """

        query = """
            SELECT
                pid,
                burst_time,
                waiting_time,
                completion_time,
                turnaround_time,
                status
            FROM ProcessTable
            WHERE status = %s
            ORDER BY pid;
        """

        return self._execute_select(query, (status,))


    def get_processes_by_waiting_time(self):
        """
        Processes ordered by waiting time.
        """

        query = """
            SELECT
                pid,
                waiting_time,
                burst_time,
                turnaround_time,
                status
            FROM ProcessTable
            ORDER BY waiting_time DESC;
        """

        return self._execute_select(query)


    def get_processes_by_turnaround_time(self):
        """
        Processes ordered by turnaround time.
        """

        query = """
            SELECT
                pid,
                waiting_time,
                burst_time,
                turnaround_time,
                status
            FROM ProcessTable
            ORDER BY turnaround_time DESC;
        """

        return self._execute_select(query)


    # ============================================================
    # PROCESS STATISTICS
    # ============================================================

    def get_process_stats(self, pid: int):
        """
        Get every RAM/statistics snapshot for a process.

        Ordered chronologically.
        """

        query = """
            SELECT
                p.pid,
                p.status,
                s.timeSnapshot,
                s.meanRAM,
                s.variance,
                s.standardDeviation,
                s.modeRAM,
                s.VelocityRAM,
                s.AcclerationRAM
            FROM ProcessTable p
            INNER JOIN ProccessStatsInfo s
                ON p.pid = s.pid
            WHERE p.pid = %s
            ORDER BY s.timeSnapshot;
        """

        return self._execute_select(query, (pid,))


    def get_all_process_stats(self):
        """
        Get statistics for all processes.
        """

        query = """
            SELECT
                p.pid,
                p.status,
                p.burst_time,
                s.timeSnapshot,
                s.meanRAM,
                s.variance,
                s.standardDeviation,
                s.modeRAM,
                s.VelocityRAM,
                s.AcclerationRAM
            FROM ProcessTable p
            INNER JOIN ProccessStatsInfo s
                ON p.pid = s.pid
            ORDER BY
                p.pid,
                s.timeSnapshot;
        """

        return self._execute_select(query)


    def get_latest_stats(self):
        """
        Get the most recent statistics snapshot
        for every process.
        """

        query = """
            SELECT
                p.pid,
                p.status,
                s.timeSnapshot,
                s.meanRAM,
                s.variance,
                s.standardDeviation,
                s.modeRAM,
                s.VelocityRAM,
                s.AcclerationRAM
            FROM ProcessTable p
            INNER JOIN ProccessStatsInfo s
                ON p.pid = s.pid
            WHERE s.timeSnapshot = (
                SELECT MAX(s2.timeSnapshot)
                FROM ProccessStatsInfo s2
                WHERE s2.pid = s.pid
            )
            ORDER BY p.pid;
        """

        return self._execute_select(query)


    def get_highest_ram_usage(self):
        """
        Processes/statistics ordered by RAM usage.
        """

        query = """
            SELECT
                p.pid,
                p.status,
                s.timeSnapshot,
                s.meanRAM
            FROM ProcessTable p
            INNER JOIN ProccessStatsInfo s
                ON p.pid = s.pid
            ORDER BY s.meanRAM DESC;
        """

        return self._execute_select(query)


    def get_highest_ram_velocity(self):
        """
        Statistics ordered by RAM velocity.
        """

        query = """
            SELECT
                p.pid,
                p.status,
                s.timeSnapshot,
                s.VelocityRAM
            FROM ProcessTable p
            INNER JOIN ProccessStatsInfo s
                ON p.pid = s.pid
            ORDER BY s.VelocityRAM DESC;
        """

        return self._execute_select(query)


    def get_highest_ram_acceleration(self):
        """
        Statistics ordered by RAM acceleration.
        """

        query = """
            SELECT
                p.pid,
                p.status,
                s.timeSnapshot,
                s.AcclerationRAM
            FROM ProcessTable p
            INNER JOIN ProccessStatsInfo s
                ON p.pid = s.pid
            ORDER BY s.AcclerationRAM DESC;
        """

        return self._execute_select(query)


    # ============================================================
    # VIRUSES
    # ============================================================

    def get_all_viruses(self):

        query = """
            SELECT
                VirusID,
                DangerLevel
            FROM Viruses
            ORDER BY VirusID;
        """

        return self._execute_select(query)


    def get_infected_processes(self):

        query = """
            SELECT
                p.pid,
                p.status,
                v.VirusID,
                v.DangerLevel,
                i.DateDiscovered
            FROM InfectedProcesses i
            INNER JOIN ProcessTable p
                ON i.pid = p.pid
            INNER JOIN Viruses v
                ON i.VirusID = v.VirusID
            ORDER BY i.DateDiscovered DESC;
        """

        return self._execute_select(query)


    def get_process_infections(self, pid: int):

        query = """
            SELECT
                p.pid,
                v.VirusID,
                v.DangerLevel,
                i.DateDiscovered
            FROM InfectedProcesses i
            INNER JOIN ProcessTable p
                ON i.pid = p.pid
            INNER JOIN Viruses v
                ON i.VirusID = v.VirusID
            WHERE p.pid = %s
            ORDER BY i.DateDiscovered DESC;
        """

        return self._execute_select(query, (pid,))


    def get_processes_infected_by_virus(self, virus_id: int):

        query = """
            SELECT
                v.VirusID,
                v.DangerLevel,
                p.pid,
                p.status,
                i.DateDiscovered
            FROM InfectedProcesses i
            INNER JOIN Viruses v
                ON i.VirusID = v.VirusID
            INNER JOIN ProcessTable p
                ON i.pid = p.pid
            WHERE v.VirusID = %s
            ORDER BY i.DateDiscovered DESC;
        """

        return self._execute_select(query, (virus_id,))


    def get_dangerous_infections(self):

        query = """
            SELECT
                p.pid,
                p.status,
                v.VirusID,
                v.DangerLevel,
                i.DateDiscovered
            FROM InfectedProcesses i
            INNER JOIN ProcessTable p
                ON i.pid = p.pid
            INNER JOIN Viruses v
                ON i.VirusID = v.VirusID
            WHERE v.DangerLevel IN ('High', 'Critical')
            ORDER BY i.DateDiscovered DESC;
        """

        return self._execute_select(query)


    def get_virus_counts(self):

        query = """
            SELECT
                p.pid,
                p.status,
                COUNT(i.VirusID) AS VirusCount
            FROM ProcessTable p
            INNER JOIN InfectedProcesses i
                ON p.pid = i.pid
            GROUP BY
                p.pid,
                p.status
            ORDER BY VirusCount DESC;
        """

        return self._execute_select(query)


    # ============================================================
    # PARENT / CHILD
    # ============================================================

    def get_parent_child_relationships(self):

        query = """
            SELECT
                parent.pid AS ParentPID,
                child.pid AS ChildPID,
                parent.status AS ParentStatus,
                child.status AS ChildStatus,
                pc.ChildCreationDate
            FROM ParentChildTable pc
            INNER JOIN ProcessTable parent
                ON pc.pidOwner = parent.pid
            INNER JOIN ProcessTable child
                ON pc.pidChild = child.pid
            ORDER BY pc.ChildCreationDate;
        """

        return self._execute_select(query)


    def get_children(self, parent_pid: int):

        query = """
            SELECT
                parent.pid AS ParentPID,
                child.pid AS ChildPID,
                child.status AS ChildStatus,
                pc.ChildCreationDate
            FROM ParentChildTable pc
            INNER JOIN ProcessTable parent
                ON pc.pidOwner = parent.pid
            INNER JOIN ProcessTable child
                ON pc.pidChild = child.pid
            WHERE parent.pid = %s
            ORDER BY pc.ChildCreationDate;
        """

        return self._execute_select(query, (parent_pid,))


    def get_parent(self, child_pid: int):

        query = """
            SELECT
                child.pid AS ChildPID,
                parent.pid AS ParentPID,
                parent.status AS ParentStatus,
                pc.ChildCreationDate
            FROM ParentChildTable pc
            INNER JOIN ProcessTable parent
                ON pc.pidOwner = parent.pid
            INNER JOIN ProcessTable child
                ON pc.pidChild = child.pid
            WHERE child.pid = %s;
        """

        return self._execute_select(query, (child_pid,))


    # ============================================================
    # MEMORY LEAKS
    # ============================================================

    def get_memory_leaks(self):

        query = """
            SELECT
                p.pid,
                p.status,
                ml.LeakedID,
                pml.TimeDiscovered
            FROM ProccessMemoryLeak pml
            INNER JOIN ProcessTable p
                ON pml.pid = p.pid
            INNER JOIN MemoryLeaked ml
                ON pml.LeakedID = ml.LeakedID
            ORDER BY pml.TimeDiscovered DESC;
        """

        return self._execute_select(query)


    def get_process_memory_leaks(self, pid: int):

        query = """
            SELECT
                p.pid,
                ml.LeakedID,
                pml.TimeDiscovered
            FROM ProccessMemoryLeak pml
            INNER JOIN ProcessTable p
                ON pml.pid = p.pid
            INNER JOIN MemoryLeaked ml
                ON pml.LeakedID = ml.LeakedID
            WHERE p.pid = %s
            ORDER BY pml.TimeDiscovered DESC;
        """

        return self._execute_select(query, (pid,))


    def get_processes_with_most_leaks(self):

        query = """
            SELECT
                p.pid,
                p.status,
                COUNT(pml.LeakedID) AS LeakCount
            FROM ProcessTable p
            INNER JOIN ProccessMemoryLeak pml
                ON p.pid = pml.pid
            GROUP BY
                p.pid,
                p.status
            ORDER BY LeakCount DESC;
        """

        return self._execute_select(query)


    # ============================================================
    # INVALID MEMORY ACCESS
    # ============================================================

    def get_invalid_memory_accesses(self):

        query = """
            SELECT
                p.pid,
                p.status,
                im.memID,
                ipa.TimeDiscovered
            FROM InvalidProccessMemoryAccess ipa
            INNER JOIN ProcessTable p
                ON ipa.pid = p.pid
            INNER JOIN InvalidMemory im
                ON ipa.memID = im.memID
            ORDER BY ipa.TimeDiscovered DESC;
        """

        return self._execute_select(query)


    def get_process_invalid_memory_accesses(self, pid: int):

        query = """
            SELECT
                p.pid,
                im.memID,
                ipa.TimeDiscovered
            FROM InvalidProccessMemoryAccess ipa
            INNER JOIN ProcessTable p
                ON ipa.pid = p.pid
            INNER JOIN InvalidMemory im
                ON ipa.memID = im.memID
            WHERE p.pid = %s
            ORDER BY ipa.TimeDiscovered DESC;
        """

        return self._execute_select(query, (pid,))


    def get_processes_with_most_invalid_accesses(self):

        query = """
            SELECT
                p.pid,
                p.status,
                COUNT(ipa.memID) AS InvalidAccessCount
            FROM ProcessTable p
            INNER JOIN InvalidProccessMemoryAccess ipa
                ON p.pid = ipa.pid
            GROUP BY
                p.pid,
                p.status
            ORDER BY InvalidAccessCount DESC;
        """

        return self._execute_select(query)


    # ============================================================
    # INSERTIONS
    # ============================================================

    def insert_process(
        self,
        pid: int,
        burst_time: float,
        status: str,
        waiting_time: Optional[float] = None,
        completion_time: Optional[float] = None,
        turnaround_time: Optional[float] = None
    ):

        query = """
            INSERT INTO ProcessTable
            (
                pid,
                burst_time,
                waiting_time,
                completion_time,
                turnaround_time,
                status
            )
            VALUES (%s, %s, %s, %s, %s, %s);
        """

        return self._execute_insert(
            query,
            (
                pid,
                burst_time,
                waiting_time,
                completion_time,
                turnaround_time,
                status
            )
        )


    def insert_process_stats(
        self,
        pid: int,
        time_snapshot: datetime,
        mean_ram: float,
        variance: float,
        standard_deviation: float,
        mode_ram: float,
        velocity_ram: float,
        acceleration_ram: float
    ):

        query = """
            INSERT INTO ProccessStatsInfo
            (
                pid,
                timeSnapshot,
                meanRAM,
                variance,
                standardDeviation,
                modeRAM,
                VelocityRAM,
                AcclerationRAM
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
        """

        return self._execute_insert(
            query,
            (
                pid,
                time_snapshot,
                mean_ram,
                variance,
                standard_deviation,
                mode_ram,
                velocity_ram,
                acceleration_ram
            )
        )


    def insert_virus(
        self,
        virus_id: int,
        danger_level: str
    ):

        query = """
            INSERT INTO Viruses
            (
                VirusID,
                DangerLevel
            )
            VALUES (%s, %s);
        """

        return self._execute_insert(
            query,
            (
                virus_id,
                danger_level
            )
        )


    def infect_process(
        self,
        pid: int,
        virus_id: int,
        date_discovered: datetime
    ):
        """
        Create an infection relationship.

        Both pid and virus_id MUST already exist.
        MySQL foreign keys enforce this.
        """

        query = """
            INSERT INTO InfectedProcesses
            (
                pid,
                VirusID,
                DateDiscovered
            )
            VALUES (%s, %s, %s);
        """

        return self._execute_insert(
            query,
            (
                pid,
                virus_id,
                date_discovered
            )
        )


    def create_parent_child_relationship(
        self,
        parent_pid: int,
        child_pid: int,
        creation_date: datetime
    ):
        """
        Both processes must already exist.
        """

        query = """
            INSERT INTO ParentChildTable
            (
                pidOwner,
                pidChild,
                ChildCreationDate
            )
            VALUES (%s, %s, %s);
        """

        return self._execute_insert(
            query,
            (
                parent_pid,
                child_pid,
                creation_date
            )
        )


    def insert_memory_leak(
        self,
        leaked_id: int
    ):

        query = """
            INSERT INTO MemoryLeaked
            (
                LeakedID
            )
            VALUES (%s);
        """

        return self._execute_insert(
            query,
            (leaked_id,)
        )


    def register_process_memory_leak(
        self,
        pid: int,
        leaked_id: int,
        time_discovered: datetime
    ):

        query = """
            INSERT INTO ProccessMemoryLeak
            (
                pid,
                LeakedID,
                TimeDiscovered
            )
            VALUES (%s, %s, %s);
        """

        return self._execute_insert(
            query,
            (
                pid,
                leaked_id,
                time_discovered
            )
        )


    def insert_invalid_memory(
        self,
        mem_id: int
    ):

        query = """
            INSERT INTO InvalidMemory
            (
                memID
            )
            VALUES (%s);
        """

        return self._execute_insert(
            query,
            (mem_id,)
        )


    def register_invalid_memory_access(
        self,
        pid: int,
        mem_id: int,
        time_discovered: datetime
    ):

        query = """
            INSERT INTO InvalidProccessMemoryAccess
            (
                pid,
                memID,
                TimeDiscovered
            )
            VALUES (%s, %s, %s);
        """

        return self._execute_insert(
            query,
            (
                pid,
                mem_id,
                time_discovered
            )
        )


    # ============================================================
    # UPDATE FUNCTIONS
    # ============================================================

    def update_process_status(
        self,
        pid: int,
        status: str
    ):

        query = """
            UPDATE ProcessTable
            SET status = %s
            WHERE pid = %s;
        """

        return self._execute_update(
            query,
            (status, pid)
        )


    def update_process_scheduling(
        self,
        pid: int,
        burst_time: float,
        waiting_time: float,
        completion_time: float,
        turnaround_time: float
    ):

        query = """
            UPDATE ProcessTable
            SET
                burst_time = %s,
                waiting_time = %s,
                completion_time = %s,
                turnaround_time = %s
            WHERE pid = %s;
        """

        return self._execute_update(
            query,
            (
                burst_time,
                waiting_time,
                completion_time,
                turnaround_time,
                pid
            )
        )


    # ============================================================
    # FULL REPORTS
    # ============================================================

    def get_full_process_report(self):

        query = """
            SELECT
                p.pid,
                p.status,
                p.burst_time,
                p.waiting_time,
                p.completion_time,
                p.turnaround_time,

                s.timeSnapshot,
                s.meanRAM,
                s.variance,
                s.standardDeviation,
                s.modeRAM,
                s.VelocityRAM,
                s.AcclerationRAM

            FROM ProcessTable p

            LEFT JOIN ProccessStatsInfo s
                ON p.pid = s.pid

            ORDER BY
                p.pid,
                s.timeSnapshot DESC;
        """

        return self._execute_select(query)
    
    
    
def test():
    print("attempting to connect to database")
    base = Database()
    base.close()
    print("closed with  no errors")
    
test()